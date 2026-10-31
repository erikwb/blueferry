"""Qt adapter for plugin management (settings category "Plugins").

Git, pip, the plugin indexes and calls into plugins all block, so they run
on a private single-thread pool; the D-Bus worker of the controller is never
held up. QML sees one plain ``state`` map (see :meth:`PluginSettings.state`)
and calls the slots the controller forwards. An install is always two
steps: ``prepareInstall`` fills ``state.pending`` with the source, ref,
capabilities and command, and only ``confirmInstall`` runs pip.

The settings form (``state.config``) carries the ApiVersion 1.3 extras:
sections, the "Test connection" and "Sign in with …" buttons and one status
line. ``check_config`` is the synchronous pre-check the form runs on every
edit; it never touches the state, so typing does not rebuild the form. A
browser sign-in polls ConfigLoginStatus on the pool with a timer until a
final state, :data:`LOGIN_TIMEOUT_SECONDS` or until the form closes.

ApiVersion 1.6 adds the plugin's own buttons (``[ConfigAction …]``): they
run ``RunConfigAction`` on the pool with the stored settings, open a
checked ``open_uri`` and then read the settings again.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, QTimer, QUrl, Signal

from blueferry import plugin_logs
from blueferry import plugin_settings_view as view
from blueferry.i18n import _
from blueferry.plugin_api.client import PluginClient
from blueferry.plugin_api.config_flow import (
    LOGIN_POLL_SECONDS,
    LOGIN_TIMEOUT_SECONDS,
    LoginStep,
)
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.plugin_index import DEFAULT_INDEX_URL, PluginIndex, check_index_url, index_urls
from blueferry.plugin_manager import InstallError, PluginManager, PreparedInstall
from blueferry.plugin_stopper import stop_note, stop_ok
from blueferry.qt.tasks import Task


def open_url(uri: str) -> None:
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(uri))


class PluginSettings(QObject):
    changed = Signal()

    def __init__(
        self,
        *,
        manager: Callable[[], PluginManager] = PluginManager,
        index: Callable[[], PluginIndex] = PluginIndex,
        client: Callable[[PluginManifest], PluginClient] = PluginClient,
        pool: QThreadPool | None = None,
        opener: Callable[[str], None] = open_url,
        poll_ms: int = LOGIN_POLL_SECONDS * 1000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager()
        self._index = index()
        self._client = client
        self._open = opener
        self._stored: dict[str, object] = {}
        # Bumped whenever a form opens or closes: a late answer for an older
        # form must not reopen or change the one shown now.
        self._form_generation = 0
        # The running browser sign-in: (plugin id, login id, polls left).
        self._login: tuple[str, str, int] | None = None
        # Exists from the initial request onward, before a login ID arrives.
        # A new attempt also fences off late replies even if IDs are reused.
        self._login_attempt: object | None = None
        self._poll_ms = max(1, poll_ms)
        self._login_timer = QTimer(self)
        self._login_timer.setSingleShot(True)
        self._login_timer.timeout.connect(self._poll_login)
        self._pool = pool or QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._tasks: set[Task] = set()
        self._queue: list[tuple] = []
        self._manifests: dict[str, PluginManifest] = {}
        # Disabled plugins are never called (that would start them again).
        self._disabled: set[str] = set()
        self._prepared: PreparedInstall | None = None
        self._store_entries: dict[str, dict] = {}
        self._state: dict[str, Any] = {
            "loaded": False, "busy": "", "message": "", "messageOk": True,
            "plugins": [], "ignored": [], "pending": {}, "config": {},
            "store": {"loaded": False, "items": [], "problems": []},
            "indexes": [], "defaultIndex": DEFAULT_INDEX_URL,
        }

    def state(self) -> dict:
        return dict(self._state)

    # ---- plumbing ---------------------------------------------------------

    def _update(self, **values: Any) -> None:
        self._state = {**self._state, **values}
        self.changed.emit()

    def _work(
        self,
        busy: str,
        operation: Callable[[], object],
        done: Callable[[Any], None],
        *,
        keep_message: bool = False,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Run one blocking step on the pool; later steps wait their turn.

        A failure lands in the page message, or in ``on_error`` when given.
        """
        if self._state["busy"]:
            self._queue.append((busy, operation, done, keep_message, on_error))
            return
        self._update(busy=busy, **({} if keep_message else {"message": ""}))

        def finished(value: object) -> None:
            self._update(busy="")
            done(value)

        def failed(message: str) -> None:
            text = message or _("The plugin operation failed")
            if on_error is not None:
                self._update(busy="")
                on_error(text)
                return
            self._update(busy="", message=text, messageOk=False)

        def next_step() -> None:
            if self._queue and not self._state["busy"]:
                step, work, then, keep, error = self._queue.pop(0)
                self._work(step, work, then, keep_message=keep, on_error=error)

        self._start(operation, finished, failed, next_step)

    def _start(
        self,
        operation: Callable[[], object],
        done: Callable[[Any], None] | None = None,
        failed: Callable[[str], None] | None = None,
        finished: Callable[[], None] | None = None,
    ) -> None:
        """Start one task on the pool and keep it referenced until it ends.

        Every task goes through here, also the quiet ones (discarding a
        checkout, cancelling a sign-in): an unreferenced Task can be
        collected while its queued signals are still on their way to the
        UI thread, which crashes the next event loop.
        """
        task = Task(operation)
        self._tasks.add(task)

        def ended() -> None:
            self._tasks.discard(task)
            if finished is not None:
                finished()

        if done is not None:
            task.signals.done.connect(done)
        if failed is not None:
            task.signals.failed.connect(failed)
        task.signals.finished.connect(ended)
        self._pool.start(task)

    def _say(self, message: str, ok: bool = True) -> None:
        self._update(message=message, messageOk=ok)

    # ---- installed plugins --------------------------------------------------

    def load(self) -> None:
        def work() -> tuple:
            entries, ignored = self._manager.entries()
            statuses = view.plugin_statuses(entries, self._client)
            return entries, ignored, statuses, index_urls(self._manager.settings())

        def done(value: tuple) -> None:
            entries, ignored, statuses, indexes = value
            self._manifests = {entry.manifest.id: entry.manifest for entry in entries}
            self._disabled = {entry.manifest.id for entry in entries if not entry.enabled}
            self._update(
                loaded=True,
                plugins=[view.entry_row(e, statuses.get(e.manifest.id)) for e in entries],
                ignored=[{"name": name, "reason": reason} for name, reason in ignored],
                indexes=indexes,
            )

        self._work("list", work, done, keep_message=True)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if not enabled:
            # Disabling is a kill switch: close its form, stop a sign-in.
            self._disabled.add(plugin_id)
            if self._state["config"].get("id") == plugin_id:
                self.close_config()

        def done(outcome: Any) -> None:
            if note := stop_note(outcome, restarts=False):
                self._say(note, stop_ok(outcome))
            self._reload()

        self._work("enable", lambda: self._manager.set_enabled(plugin_id, enabled), done)

    def _reload(self) -> None:
        self.load()

    # ---- install, update, remove ----------------------------------------------

    def _show_pending(self, kind: str, prepared: PreparedInstall | None) -> None:
        if prepared is None:
            self._say(_("The plugin is up to date."))
            return
        self._prepared = prepared
        self._update(pending={
            "kind": kind, "id": prepared.manifest.id, "name": prepared.manifest.name,
            "rows": view.summary_rows(prepared),
        })

    def prepare_install(self, url: str, ref: str = "") -> None:
        self.cancel_install()
        self._work("prepare", lambda: self._manager.prepare(url.strip(), ref.strip() or None),
                   lambda prepared: self._show_pending("install", prepared))

    def install_from_store(self, plugin_id: str) -> None:
        card = self._store_entries.get(plugin_id)
        if card is None or not card["installable"]:
            return
        if card["state"] == "update" and plugin_id in self._manifests:
            self.prepare_update(plugin_id)
            return
        self.prepare_install(card["repo"], card["ref"])

    def prepare_update(self, plugin_id: str) -> None:
        self.cancel_install()
        self._work("prepare", lambda: self._manager.prepare_update(plugin_id),
                   lambda prepared: self._show_pending("update", prepared))

    def confirm_install(self) -> None:
        prepared = self._prepared
        if prepared is None:
            return
        self._prepared = None
        kind = self._state["pending"].get("kind", "install")
        self._update(pending={})

        def done(record: Any) -> None:
            text = (_("Updated {name} to {ref}.") if kind == "update"
                    else _("Installed {name} {ref}.")).format(
                name=prepared.manifest.name, ref=record.ref_label,
            )
            note = stop_note(prepared.stop)
            self._say(f"{text} {note}" if note else text,
                      stop_ok(prepared.stop))
            self._reload()
            self.load_store(False)

        self._work("install", lambda: self._manager.commit(prepared), done)

    def cancel_install(self) -> None:
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            # Deleting the checkout touches the disk; keep it off the UI thread.
            self._start(lambda: self._manager.discard(prepared))
        if self._state["pending"]:
            self._update(pending={})

    def remove(self, plugin_id: str) -> None:
        if self._state["config"].get("id") == plugin_id:
            self.close_config()

        def done(removal: Any) -> None:
            text = _("Removed the plugin. Its own settings and keyring entries stay.")
            note = stop_note(removal.stop, restarts=False)
            self._say(f"{text} {note}" if note else text, stop_ok(removal.stop))
            self._reload()

        self._work("remove", lambda: self._manager.remove(plugin_id), done)

    # ---- settings form --------------------------------------------------------

    def _form(self, manifest: PluginManifest, plugin_id: str, values: Mapping[str, object],
              *, loaded: bool) -> dict[str, Any]:
        return {
            "id": plugin_id, "name": manifest.name, "loaded": loaded,
            "fields": view.form_fields(manifest, values),
            "groups": view.form_groups(manifest),
            "actions": view.form_actions(manifest),
            "errors": {}, "status": {}, "loadError": "",
        }

    def load_config(self, plugin_id: str, *, keep_status: bool = False) -> None:
        manifest = self._manifests.get(plugin_id)
        if manifest is None or not manifest.config or plugin_id in self._disabled:
            return
        status = self._state["config"].get("status", {}) if keep_status else {}
        if not keep_status:
            self._stop_login()

        owns = self._owner(plugin_id)

        def done(values: Mapping[str, object]) -> None:
            if not owns():
                return
            self._stored = dict(values)
            self._update(config={**self._form(manifest, plugin_id, values, loaded=True),
                                 "status": status})

        def failed(text: str) -> None:
            if owns():
                self._update(config={**self._state["config"], "loadError": text})

        self._stored = {}
        if keep_status:
            # An action or sign-in may have changed the saved settings.
            # The old fields must stay unavailable until a fresh read succeeds.
            self._update(config={**self._state["config"], "loaded": False, "loadError": ""})
        else:
            self._update(config=self._form(manifest, plugin_id, {}, loaded=False))
        self._work("config", lambda: self._client(manifest).get_config(), done, on_error=failed)

    def _current(self, plugin_id: str) -> PluginManifest | None:
        if self._state["config"].get("id") != plugin_id or plugin_id in self._disabled:
            return None
        return self._manifests.get(plugin_id)

    def _owner(self, plugin_id: str) -> Callable[[], bool]:
        """Whether the form shown now is still the one open at this call."""
        self._form_generation += 1
        generation = self._form_generation

        def owns() -> bool:
            return generation == self._form_generation and self._current(plugin_id) is not None

        return owns

    def check_config(self, plugin_id: str, raw: Mapping[str, object]) -> dict[str, Any]:
        """The form's pre-check on every edit; does not change the state."""
        manifest = self._current(plugin_id)
        if manifest is None or not self._state["config"].get("loaded"):
            return {"errors": {}, "visible": [], "valid": False}
        return view.check_form(manifest, self._stored, raw)

    def _set_status(self, kind: str, ok: bool, text: str, *, pending: bool = False) -> None:
        if not self._state["config"]:
            return
        self._update(config={**self._state["config"], "status": {
            "kind": kind, "ok": ok, "text": text, "pending": pending,
        }})

    def save_config(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        manifest = self._current(plugin_id)
        if manifest is None or not self._state["config"].get("loaded"):
            return
        values, errors = view.parse_form(manifest, raw)
        if errors:
            self._update(config={**self._state["config"], "errors": errors})
            return

        def done(result: Any) -> None:
            # Only touch the form while it is still this plugin's.
            current = self._current(plugin_id) is not None
            if result.ok:
                self._say(_("Saved the settings of {name}.").format(name=manifest.name))
                if current:
                    self.close_config()
                self._reload()
            elif current:
                self._update(config={**self._state["config"], "errors": dict(result.errors)})

        self._work("config", lambda: self._client(manifest).set_config(values), done)

    def test_config(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        """"Test connection": the plugin checks the typed values, nothing is saved."""
        manifest = self._current(plugin_id)
        if (manifest is None or not manifest.config_test
                or not self._state["config"].get("loaded")):
            return
        values, errors = view.parse_form(manifest, raw)
        if errors:
            self._update(config={**self._state["config"], "errors": errors})
            return
        self._set_status("test", True, _("Testing the connection…"), pending=True)

        def done(result: Any) -> None:
            # Only into the form that asked: not into another plugin's form
            # opened meanwhile, and not as a new form after closing it.
            if self._current(plugin_id) is None:
                return
            self._update(config={
                **self._state["config"], "errors": dict(result.errors),
                "status": {"kind": "test", "ok": result.ok, "text": result.message,
                           "pending": False},
            })

        def failed(text: str) -> None:
            if self._current(plugin_id) is not None:
                self._set_status("test", False, text)

        self._work("test", lambda: self._client(manifest).test_config(values), done,
                   on_error=failed)

    def sign_in(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        """"Sign in with …": start the plugin's browser flow and poll it."""
        manifest = self._current(plugin_id)
        if manifest is None or not self._state["config"].get("loaded"):
            return
        self._stop_login()
        attempt = self._login_attempt = object()
        values = view.login_values(manifest, raw)
        self._set_status("login", True, view.login_text("pending"), pending=True)

        def current() -> bool:
            return self._login_attempt is attempt and self._current(plugin_id) is not None

        def start() -> LoginStep:
            if not current():
                return LoginStep("cancelled")
            return self._client(manifest).config_login(values)

        def done(step: LoginStep) -> None:
            if not current():
                # Cancelled, closed or replaced: discard the answer and end
                # any flow the plugin started before we could cancel it.
                if step.state == "open":
                    client = self._client(manifest)
                    self._start(lambda: client.config_login_cancel(step.login_id))
                return
            if step.state == "open":
                self._open(step.open_uri)
                self._login = (plugin_id, step.login_id,
                               max(1, LOGIN_TIMEOUT_SECONDS * 1000 // self._poll_ms))
                self._set_status("login", True, view.login_text("pending", step.message),
                                 pending=True)
                self._login_timer.start(self._poll_ms)
            else:
                self._login_finished(plugin_id, step)

        def failed(text: str) -> None:
            if current():
                self._login_attempt = None
                self._set_status("login", False, text)

        self._work("login", start, done, on_error=failed)

    def _poll_login(self) -> None:
        login = self._login
        if login is None:
            return
        plugin_id, login_id, left = login
        manifest = self._current(plugin_id)
        if manifest is None:
            self._stop_login()
            return
        if left <= 0:
            self._stop_login()
            self._set_status("login", False, view.login_text("expired"))
            return
        self._login = (plugin_id, login_id, left - 1)
        attempt = self._login_attempt

        def done(step: LoginStep) -> None:
            if self._login is None or self._login_attempt is not attempt:
                return
            if step.final:
                self._login = None
                self._login_finished(plugin_id, step)
            else:
                self._login_timer.start(self._poll_ms)

        def failed(message: str) -> None:
            if self._login is not None and self._login_attempt is attempt:
                self._login = None
                self._login_attempt = None
                self._set_status("login", False, message or view.login_text("error"))

        self._start(lambda: self._client(manifest).config_login_status(login_id), done, failed)

    def _login_finished(self, plugin_id: str, step: LoginStep) -> None:
        self._login_attempt = None
        ok = step.state == "done"
        self._set_status("login", ok, view.login_text(step.state, step.message))
        if ok and self._current(plugin_id) is not None:
            self.load_config(plugin_id, keep_status=True)
            self._reload()

    def cancel_sign_in(self) -> None:
        active = self._login_attempt is not None
        self._stop_login()
        if active:
            self._set_status("login", False, view.login_text("cancelled"))

    def _stop_login(self) -> tuple[str, str, int] | None:
        self._login_attempt = None
        login, self._login = self._login, None
        self._login_timer.stop()
        if login is not None:
            manifest = self._manifests.get(login[0])
            if manifest is not None:
                client = self._client(manifest)
                self._start(lambda: client.config_login_cancel(login[1]))
        return login

    def run_action(self, plugin_id: str, action_id: str) -> None:
        """A ``[ConfigAction …]`` button: the plugin works with its stored
        settings, so the form offers it only without unsaved edits."""
        manifest = self._current(plugin_id)
        if manifest is None or manifest.config_action(action_id) is None:
            return
        if (not self._state["config"].get("loaded")
                or self._state["config"].get("status", {}).get("pending")):
            return
        generation = self._form_generation

        def current() -> bool:
            return generation == self._form_generation and self._current(plugin_id) is not None

        def run() -> Any:
            # A queued action must not activate a plugin after its form was
            # closed or replaced, including a new form for the same plugin.
            if not current():
                return None
            return self._client(manifest).run_config_action(action_id)

        self._set_status("action", True, _("Working…"), pending=True)

        def done(result: Any) -> None:
            # The user may have closed this form, opened another one or
            # disabled the plugin meanwhile: the answer, and any page it
            # asks to open, belongs to this form only.
            if not current() or result is None:
                return
            if result.ok and result.open_uri:
                self._open(result.open_uri)
            self._set_status("action", result.ok,
                             view.config_action_text(result.ok, result.message, manifest.name))
            # The action may have changed settings or the plugin's status.
            self.load_config(plugin_id, keep_status=True)
            self._reload()

        def failed(text: str) -> None:
            if current():
                self._set_status("action", False, text)

        self._work("action", run, done, on_error=failed)

    def open_help(self, plugin_id: str, key: str) -> None:
        """"Where do I find this?": the field's https HelpUrl, nothing else."""
        manifest = self._manifests.get(plugin_id)
        url = view.help_link(manifest, key) if manifest is not None else ""
        if url:
            self._open(url)

    def open_log(self, plugin_id: str) -> None:
        """"Show Log": the plugin's standard log file in the text viewer."""
        def done(path: object) -> None:
            if isinstance(path, Path):
                self._open(path.as_uri())
            else:
                self._say(_("This plugin has not written a log yet. Plugins for plugin API "
                            "1.4 start one the first time they run."), False)

        self._work("log", lambda: plugin_logs.find_log(plugin_id), done)

    def close_config(self) -> None:
        self._form_generation += 1
        self._stop_login()
        self._stored = {}
        self._update(config={})

    # ---- store --------------------------------------------------------------------

    def load_store(self, refresh: bool) -> None:
        def work() -> object:
            entries, _ignored = self._manager.entries()
            return view.load_catalog(self._manager, self._index, entries, refresh=refresh)

        def done(catalog: Any) -> None:
            cards = [view.store_row(item) for item in catalog.items]
            self._store_entries = {card["id"]: card for card in cards}
            self._update(store={
                "loaded": True, "items": cards,
                "problems": [{"index": url, "problem": text} for url, text in catalog.problems],
            })

        self._work("store", work, done, keep_message=True)

    def set_indexes(self, urls: list[str]) -> None:
        try:
            cleaned = list(dict.fromkeys(check_index_url(str(url)) for url in urls if url))
        except InstallError as error:
            self._say(str(error), False)
            return

        def done(_value: object) -> None:
            self._update(indexes=cleaned)
            self.load_store(True)

        self._work("indexes", lambda: self._manager.update_settings(indexes=cleaned), done)

    def clear_message(self) -> None:
        self._update(message="")
