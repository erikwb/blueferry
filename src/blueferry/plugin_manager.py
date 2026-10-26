"""Install, update, enable and remove plugins from Git URLs.

Layout below ``$XDG_DATA_HOME/blueferry/plugins/`` (default
``~/.local/share/blueferry/plugins/``)::

    <id>.plugin               manifest that clients discover (Exec rewritten)
    src/<id>/                 checkout of the pinned ref
    venvs/<id>-<commit>[-<install>]/  the plugin's own virtual environment
    installs.json             url, ref and commit of every managed plugin

plus ``$XDG_DATA_HOME/dbus-1/services/<bus name>.service`` for activation.
Which plugins are disabled lives in ``$XDG_CONFIG_HOME/blueferry/plugins.json``.

The CLI and the Qt settings may run at the same time, so preparing,
committing, removing and enabling take an exclusive ``flock`` on
``plugins/.lock``; a second change waits a few seconds and then fails with
a message instead of interleaving. ``installs.json`` is validated on every
read: an entry with an odd id, URL, ref, commit or a venv outside
``venvs/`` is ignored, so a damaged file can never point a removal or the
stopper somewhere else.

Installing is two steps so every client can ask first: :meth:`prepare`
resolves the ref, clones it and reads the manifest without running any of
the plugin's code; :meth:`commit` builds the venv, installs the package and
writes the manifest and the service file. Everything here blocks (git,
pip): call it from a worker thread or the CLI. Git and venv access go
through an injectable :class:`Runner`; tests use fakes.

A plugin process keeps running after its venv is replaced or deleted, so an
update, a removal and disabling end the process that still owns the
plugin's bus name through an injectable stopper
(:class:`blueferry.plugin_stopper.BusPluginStopper` by default); D-Bus
activation starts the new code on next use.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from blueferry import __version__
from blueferry.plugin_api.client import plain_text
from blueferry.plugin_api.manifest import (
    ManifestError,
    PluginManifest,
    bus_name_for,
    default_directories,
    discover,
    parse_manifest,
    valid_plugin_id,
)
from blueferry.plugin_prefs import config_path, disabled_from
from blueferry.plugin_stopper import UNMANAGED, BusPluginStopper, PluginStopper, StopOutcome

GIT_TIMEOUT_SEC = 120.0
PIP_TIMEOUT_SEC = 900.0
MAX_LOG_LINES = 20
LOCK_WAIT_SEC = 5.0
_HTTPS_GIT = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?/[A-Za-z0-9._~/-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_VERSION_TAG = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")
_PYTHONS = frozenset({"python", "python3"})


class InstallError(Exception):
    """An install, update or removal failed; the message says why."""


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> str:
        """Run ``argv`` (no shell) and return stdout; raise InstallError on failure."""


class SubprocessRunner:
    def run(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false",
                   PIP_DISABLE_PIP_VERSION_CHECK="1")
        try:
            completed = subprocess.run(  # nosec B603
                list(argv), cwd=cwd, env=env, capture_output=True, text=True,
                timeout=timeout, check=False, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallError(f"{Path(argv[0]).name} failed: {type(error).__name__}") from None
        if completed.returncode != 0:
            # The last line of git's or pip's output reaches every client:
            # one line of printable plain text, never terminal escapes.
            lines = (completed.stderr or completed.stdout or "").strip().splitlines()
            reason = plain_text(lines[-1], 300) if lines else ""
            reason = reason or f"exit status {completed.returncode}"
            raise InstallError(f"{Path(argv[0]).name} failed: {reason}")
        return completed.stdout


def data_dir() -> Path:
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share",
    )
    return Path(home)


def check_url(url: str) -> str:
    url = url.strip()
    if len(url) > 300 or not _HTTPS_GIT.fullmatch(url) or "/../" in url or url.endswith("/.."):
        raise InstallError("only https:// Git URLs can be installed")
    return url


def short(commit: str) -> str:
    return commit[:12]


@dataclass(frozen=True, slots=True)
class InstallRecord:
    id: str
    url: str
    ref: str          # tag name, or the commit when the default branch was pinned
    commit: str
    venv: str
    installed_at: float = 0.0

    @property
    def ref_label(self) -> str:
        return short(self.ref) if _COMMIT.fullmatch(self.ref) else self.ref


@dataclass(slots=True)
class PreparedInstall:
    url: str
    ref: str
    commit: str
    manifest: PluginManifest
    manifest_text: str
    staging: Path
    previous: InstallRecord | None = None
    # A manifest with this id that BlueFerry did not install (shadowed or replaced).
    unmanaged: PluginManifest | None = None
    changes: list[str] = field(default_factory=list)
    # Set by PluginManager.commit: what happened to a still running old process.
    stop: StopOutcome | None = None

    def summary(self) -> list[tuple[str, str]]:
        """Label/value rows every client shows before asking."""
        plugin = self.manifest
        rows = [
            ("Plugin", f"{plugin.name} {plugin.version} ({plugin.id})"),
            ("Source", self.url),
            ("Ref", f"{self.ref if not _COMMIT.fullmatch(self.ref) else short(self.ref)}"
                    f" (commit {short(self.commit)})"),
            ("Capabilities", ", ".join(plugin.capabilities)),
            ("Runs", shlex.join(plugin.exec)),
        ]
        if plugin.config:
            rows.append(("Settings", ", ".join(f.label for f in plugin.config)))
        if self.previous is not None:
            rows.append(("Replaces", f"{self.previous.ref_label} "
                                     f"(commit {short(self.previous.commit)})"))
        elif self.unmanaged is not None and self.unmanaged.path is not None:
            rows.append(("Replaces", f"manually installed manifest {self.unmanaged.path}"))
        return rows


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """One plugin as the settings UIs list it."""

    manifest: PluginManifest
    record: InstallRecord | None
    enabled: bool

    @property
    def managed(self) -> bool:
        return self.record is not None

    @property
    def source(self) -> str:
        if self.record is not None:
            return self.record.url
        return self.manifest.source or self.manifest.homepage


@dataclass(frozen=True, slots=True)
class Removal:
    """What :meth:`PluginManager.remove` deleted and stopped."""

    paths: list[Path]
    stop: StopOutcome | None = None


def _version_key(tag: str) -> tuple[int, ...]:
    match = _VERSION_TAG.fullmatch(tag)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


class PluginManager:
    def __init__(
        self,
        *,
        data_home: Path | None = None,
        settings_path: Path | None = None,
        runner: Runner | None = None,
        python: str = sys.executable,
        clock: Callable[[], float] = time.time,
        blueferry_version: str = __version__,
        stopper: PluginStopper | None = None,
    ) -> None:
        self.data_home = data_home or data_dir()
        self.root = self.data_home / "blueferry" / "plugins"
        self.settings_path = settings_path or config_path()
        self.runner = runner or SubprocessRunner()
        self.python = python
        self.clock = clock
        self.blueferry_version = blueferry_version
        self.stopper: PluginStopper = stopper or BusPluginStopper()
        self._thread_lock = threading.RLock()
        self._lock_depth = 0

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive across processes (flock) and threads; re-entrant."""
        with self._thread_lock:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                deadline = time.monotonic() + LOCK_WAIT_SEC
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise InstallError(
                                "another plugin install or change is running; try again "
                                "when it has finished") from None
                        # Blocking wait is fine here: the manager runs in the
                        # CLI or on a client's worker pool, never on the
                        # daemon's or a UI's main loop (the daemon does not
                        # import this module; tools/lint_mainloop.py on
                        # chore/mainloop-guard would flag it if it did).
                        time.sleep(0.1)
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ---- paths and records -----------------------------------------------------

    def source_dir(self, plugin_id: str) -> Path:
        return self.root / "src" / plugin_id

    def manifest_path(self, plugin_id: str) -> Path:
        return self.root / f"{plugin_id}.plugin"

    def service_path(self, manifest: PluginManifest) -> Path:
        return self.data_home / "dbus-1" / "services" / f"{manifest.bus_name}.service"

    def records(self) -> dict[str, InstallRecord]:
        """Valid entries of ``installs.json``; anything odd is left out."""
        raw = _read_json(self.root / "installs.json")
        plugins = raw.get("plugins")
        records: dict[str, InstallRecord] = {}
        if not isinstance(plugins, dict):
            return records
        for plugin_id, value in plugins.items():
            record = self._checked_record(plugin_id, value)
            if record is not None:
                records[record.id] = record
        return records

    def _checked_record(self, plugin_id: object, value: object) -> InstallRecord | None:
        if not isinstance(value, dict):
            return None
        try:
            record = InstallRecord(**value)
        except TypeError:
            return None
        if not all(isinstance(text, str) for text in
                   (record.id, record.url, record.ref, record.commit, record.venv)):
            return None
        if not isinstance(record.installed_at, (int, float)) or isinstance(
                record.installed_at, bool):
            return None
        if record.id != plugin_id or not valid_plugin_id(record.id):
            return None
        try:
            check_url(record.url)
        except InstallError:
            return None
        if not _COMMIT.fullmatch(record.commit):
            return None
        if not (_COMMIT.fullmatch(record.ref) or (_TAG.fullmatch(record.ref)
                                                  and ".." not in record.ref)):
            return None
        # Only a venv commit() could have made; reinstalling may need a new
        # path so the currently installed runtime remains usable on failure.
        base = str(self.venv_path(record.id, record.commit))
        if not re.fullmatch(re.escape(base) + r"(?:-[0-9a-f]{32})?", record.venv):
            return None
        return record

    def venv_path(self, plugin_id: str, commit: str) -> Path:
        return self.root / "venvs" / f"{plugin_id}-{short(commit)}"

    def _plugin_venvs(self, plugin_id: str) -> list[Path]:
        # IDs may contain hyphens: id-* also matches another plugin named
        # id-extra. Require the complete runtime suffix, not just a prefix.
        pattern = re.compile(re.escape(plugin_id) + r"-[0-9a-f]{12}(?:-[0-9a-f]{32})?")
        return sorted(path for path in (self.root / "venvs").glob(f"{plugin_id}-*")
                      if pattern.fullmatch(path.name))

    def _new_venv_path(self, plugin_id: str, commit: str) -> Path:
        path = self.venv_path(plugin_id, commit)
        while path.exists() or path.is_symlink():
            path = self.venv_path(plugin_id, commit).with_name(
                f"{plugin_id}-{short(commit)}-{uuid.uuid4().hex}")
        return path

    def _save_records(self, records: Mapping[str, InstallRecord]) -> None:
        _write_json(self.root / "installs.json", {
            "plugins": {key: asdict(value) for key, value in sorted(records.items())},
        }, private=False)

    def settings(self) -> dict:
        return _read_json(self.settings_path)

    def update_settings(self, **values: object) -> None:
        with self._locked():
            current = self.settings()
            current.update(values)
            _write_json(self.settings_path, current, private=True)

    def disabled(self) -> frozenset[str]:
        return disabled_from(self.settings())

    def set_enabled(self, plugin_id: str, enabled: bool) -> StopOutcome | None:
        """Enable or disable; disabling stops a running managed plugin.

        Disabling is a kill switch for BlueFerry's side: no client calls a
        disabled plugin, and the daemon drops its popups at once. A plugin
        BlueFerry did not install has no venv to check a running process
        against, so it is not stopped (outcome ``unmanaged``).
        """
        with self._locked():
            disabled = set(self.disabled())
            (disabled.discard if enabled else disabled.add)(plugin_id)
            self.update_settings(disabled=sorted(disabled))
            record = self.records().get(plugin_id)
            if enabled:
                return None
            if record is None:
                return StopOutcome(UNMANAGED)
            return self._stop_old(plugin_id, [Path(record.venv)])

    def _stop_old(self, plugin_id: str, venvs: Sequence[Path]) -> StopOutcome | None:
        """End the bus-name owner if it runs from one of ``venvs``."""
        if not venvs:
            return None
        return self.stopper(bus_name_for(plugin_id), venvs)

    def entries(self) -> tuple[list[PluginEntry], list[tuple[str, str]]]:
        """Discovered plugins (managed or not) and ignored manifests."""
        found = discover(blueferry_version=self.blueferry_version)
        records, disabled = self.records(), self.disabled()
        entries = [
            PluginEntry(plugin, records.get(plugin.id), plugin.id not in disabled)
            for plugin in found.plugins
        ]
        return entries, list(found.ignored)

    # ---- resolving refs -----------------------------------------------------------

    def _ls_remote(self, *args: str) -> list[tuple[str, str]]:
        output = self.runner.run(
            ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
             "ls-remote", *args], timeout=GIT_TIMEOUT_SEC,
        )
        refs = []
        for line in output.splitlines():
            commit, _, name = line.partition("\t")
            if _COMMIT.fullmatch(commit.strip()):
                refs.append((commit.strip(), name.strip()))
        return refs

    def resolve(self, url: str, ref: str | None = None) -> tuple[str, str]:
        """``(ref, commit)``: the given tag or commit, else the newest version
        tag, else the default branch's HEAD pinned as a commit."""
        url = check_url(url)
        if ref:
            ref = ref.strip()
            if _COMMIT.fullmatch(ref):
                return ref, ref
            if not _TAG.fullmatch(ref) or ".." in ref:
                raise InstallError("--ref must be a tag or a full 40-character commit")
            tags = self._tags(url)
            if ref not in tags:
                raise InstallError(f"the repository has no tag {ref}")
            return ref, tags[ref]
        tags = self._tags(url)
        versions = [tag for tag in tags if _version_key(tag)]
        if versions:
            newest = max(versions, key=_version_key)
            return newest, tags[newest]
        heads = [commit for commit, name in self._ls_remote("--", url, "HEAD") if name == "HEAD"]
        if not heads:
            raise InstallError("the repository has no tags and no default branch")
        return heads[0], heads[0]

    def _tags(self, url: str) -> dict[str, str]:
        tags: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for commit, name in self._ls_remote("--tags", "--", url):
            if not name.startswith("refs/tags/"):
                continue
            tag = name[len("refs/tags/"):]
            if tag.endswith("^{}"):
                peeled[tag[:-3]] = commit
            elif _TAG.fullmatch(tag):
                tags[tag] = commit
        # Annotated tags: use the commit they point to.
        return {tag: peeled.get(tag, commit) for tag, commit in tags.items()}

    # ---- install --------------------------------------------------------------------

    def _check_bus_name(self, plugin: PluginManifest) -> None:
        # Include our explicit data_home even when it differs from the process
        # environment, and records whose manifests are missing or damaged.
        found = discover((self.root, *default_directories()),
                         blueferry_version=self.blueferry_version)
        ids = set(self.records()) | {p.id for p in (*found.plugins, *found.conflicts)}
        for other_id in sorted(ids):
            if other_id != plugin.id and bus_name_for(other_id) == plugin.bus_name:
                raise InstallError(f"{plugin.id} shares its D-Bus name with {other_id}; "
                                   "remove the conflicting plugin first")

    def prepare(self, url: str, ref: str | None = None) -> PreparedInstall:
        """Clone the pinned ref and read its manifest. Runs no plugin code."""
        with self._locked():
            return self._prepare(url, ref)

    def _prepare(self, url: str, ref: str | None) -> PreparedInstall:
        url = check_url(url)
        ref, commit = self.resolve(url, ref)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root / "src"))
        try:
            git = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                   "-c", "core.hooksPath=/dev/null"]
            self.runner.run([*git, "clone", "--quiet", "--no-checkout", "--", url, str(staging)],
                            timeout=GIT_TIMEOUT_SEC)
            self.runner.run([*git, "-C", str(staging), "checkout", "--quiet", "--detach", commit],
                            timeout=GIT_TIMEOUT_SEC)
            head = self.runner.run(["git", "-C", str(staging), "rev-parse", "HEAD"],
                                   timeout=GIT_TIMEOUT_SEC).strip()
            if head != commit:
                raise InstallError("the checkout does not match the resolved commit")
            text = _manifest_text(staging)
            try:
                manifest = parse_manifest(text, blueferry_version=self.blueferry_version)
            except ManifestError as error:
                raise InstallError(f"the plugin's manifest is unusable: {error}") from None
            self._check_bus_name(manifest)
            for argv in (manifest.exec, manifest.cli):
                if argv:
                    _venv_program(Path("venv"), argv, installed=False)  # refuse before asking
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        previous = self.records().get(manifest.id)
        unmanaged = None
        if previous is None:
            found = discover(blueferry_version=self.blueferry_version).find(manifest.id)
            unmanaged = found
        return PreparedInstall(url, ref, commit, manifest, text, staging, previous, unmanaged)

    def discard(self, prepared: PreparedInstall) -> None:
        """Drop a prepared checkout the user did not confirm."""
        shutil.rmtree(prepared.staging, ignore_errors=True)

    def commit(self, prepared: PreparedInstall) -> InstallRecord:
        """Build the venv, install the package, publish manifest and service."""
        with self._locked():
            return self._commit(prepared)

    def _commit(self, prepared: PreparedInstall) -> InstallRecord:
        plugin = prepared.manifest
        # The user may have left the confirmation open while another client
        # installed, updated, reinstalled or removed this plugin. The lock
        # protects each operation, not the time spent awaiting confirmation.
        if self.records().get(plugin.id) != prepared.previous:
            raise InstallError("the installed plugin changed since preparation; "
                               "prepare the installation again")
        self._check_bus_name(plugin)
        target = self.source_dir(plugin.id)
        old_venvs = self._plugin_venvs(plugin.id)
        if prepared.previous is not None and Path(prepared.previous.venv) not in old_venvs:
            old_venvs.append(Path(prepared.previous.venv))
        backup = None
        if target.exists():
            backup = target.with_name(f".old-{plugin.id}-{os.getpid()}")
            shutil.rmtree(backup, ignore_errors=True)
            os.replace(target, backup)
        os.replace(prepared.staging, target)
        # Venv scripts embed absolute paths, so build at a fresh final path
        # instead of moving a staging venv or overwriting a working install.
        venv = self._new_venv_path(plugin.id, prepared.commit)
        try:
            venv.parent.mkdir(parents=True, exist_ok=True)
            # PyGObject and dbus-python come from the distribution.
            self.runner.run([self.python, "-m", "venv", "--system-site-packages", str(venv)],
                            timeout=PIP_TIMEOUT_SEC)
            python = str(venv / "bin" / "python")
            self.runner.run([python, "-m", "pip", "install", "--quiet", str(target)],
                            timeout=PIP_TIMEOUT_SEC)
            self.runner.run([python, "-c", "import blueferry.plugin_api"],
                            timeout=GIT_TIMEOUT_SEC)
            exec_argv = _venv_program(venv, plugin.exec)
            cli_argv = _venv_program(venv, plugin.cli) if plugin.cli else ()
            text = _rewrite(prepared.manifest_text, exec_argv, cli_argv)
            try:
                parse_manifest(text, blueferry_version=self.blueferry_version)
            except ManifestError as error:
                raise InstallError(f"the rewritten manifest is unusable: {error}") from None
            record = InstallRecord(plugin.id, prepared.url, prepared.ref, prepared.commit,
                                   str(venv), self.clock())
            records = self.records()
            records[plugin.id] = record
            # Keep the previous manifest/service/record together if any
            # publication fails after the package has been built.
            with _restore_on_failure([
                self.manifest_path(plugin.id), self.service_path(plugin),
                self.root / "installs.json",
            ]):
                _write_text(self.manifest_path(plugin.id), text)
                _write_text(
                    self.service_path(plugin),
                    f"[D-BUS Service]\nName={plugin.bus_name}\nExec={shlex.join(exec_argv)}\n",
                )
                self._save_records(records)
        except BaseException:
            shutil.rmtree(venv, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)
            if backup is not None:
                os.replace(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        for old in old_venvs:
            if old != venv:
                shutil.rmtree(old, ignore_errors=True)
        prepared.stop = self._stop_old(plugin.id, old_venvs)
        return record

    # ---- update and removal ------------------------------------------------------------

    def check_update(self, plugin_id: str) -> tuple[InstallRecord, str, str]:
        """``(record, ref, commit)`` of what an update would install."""
        record = self.records().get(plugin_id)
        if record is None:
            raise InstallError(f"{plugin_id} was not installed with `blueferry plugins install`")
        if _COMMIT.fullmatch(record.ref):
            ref, commit = self.resolve(record.url)
        else:
            tags = self._tags(record.url)
            versions = [tag for tag in tags if _version_key(tag)]
            if versions:
                ref = max(versions, key=_version_key)
                if _version_key(ref) < _version_key(record.ref):
                    ref = record.ref
                commit = tags.get(ref, record.commit)
            else:
                ref, commit = record.ref, tags.get(record.ref, record.commit)
        return record, ref, commit

    def prepare_update(self, plugin_id: str) -> PreparedInstall | None:
        """The pending update with its commit log, or None when up to date."""
        record, ref, commit = self.check_update(plugin_id)
        if commit == record.commit:
            return None
        prepared = self.prepare(record.url, ref)
        try:
            log = self.runner.run(
                ["git", "-C", str(prepared.staging), "log", "--oneline", "--no-decorate",
                 f"--max-count={MAX_LOG_LINES}", f"{record.commit}..{prepared.commit}"],
                timeout=GIT_TIMEOUT_SEC,
            )
            prepared.changes = log.splitlines()[:MAX_LOG_LINES]
        except InstallError:
            prepared.changes = []  # the old commit may be gone after a force-push
        return prepared

    def remove(self, plugin_id: str) -> Removal:
        """Remove a managed plugin and stop it; its settings and keyring entries stay."""
        with self._locked():
            return self._remove(plugin_id)

    def _remove(self, plugin_id: str) -> Removal:
        records = self.records()
        record = records.get(plugin_id)
        if record is None:
            raise InstallError(f"{plugin_id} was not installed with `blueferry plugins install`")
        removed: list[Path] = []
        manifest_file = self.manifest_path(plugin_id)
        try:
            manifest = parse_manifest(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ManifestError):
            manifest = None
        paths = [manifest_file, self.source_dir(plugin_id)]
        if manifest is not None:
            paths.append(self.service_path(manifest))
        venvs = self._plugin_venvs(plugin_id)
        if Path(record.venv) not in venvs:
            venvs.append(Path(record.venv))
        paths.extend(venvs)
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
                removed.append(path)
        del records[plugin_id]
        self._save_records(records)
        if plugin_id in self.disabled():
            self.set_enabled(plugin_id, True)
        return Removal(removed, self._stop_old(plugin_id, venvs))


# ---- helpers --------------------------------------------------------------------------


MAX_MANIFEST_BYTES = 16 * 1024


@contextmanager
def _restore_on_failure(paths: Sequence[Path]) -> Iterator[None]:
    """Retain metadata backups until the complete install is published."""
    backups: list[tuple[Path, Path | None]] = []
    try:
        for path in paths:
            backup = None
            if path.exists():
                fd, name = tempfile.mkstemp(prefix=f".{path.name}.backup-", dir=path.parent)
                os.close(fd)
                backup = Path(name)
                try:
                    shutil.copy2(path, backup)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            backups.append((path, backup))
        yield
    except BaseException:
        for path, backup in reversed(backups):
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
        raise
    finally:
        for _path, backup in backups:
            if backup is not None:
                backup.unlink(missing_ok=True)


def _manifest_text(checkout: Path) -> str:
    candidates = sorted(checkout.glob("data/*.plugin")) or sorted(
        checkout.glob("src/*/*.plugin")) or sorted(checkout.glob("*.plugin"))
    if len(candidates) != 1:
        raise InstallError("the repository must contain exactly one *.plugin manifest "
                           "(data/, src/<package>/ or the top level)")
    path = candidates[0]
    # Neither the manifest nor a folder on the way to it may be a symlink:
    # a link could point the glob at a file outside the checkout.
    current = checkout
    for part in path.relative_to(checkout).parts:
        current = current / part
        if current.is_symlink():
            raise InstallError("the manifest is not a regular file inside the repository")
    if not path.is_file():
        raise InstallError("the manifest is not a regular file")
    with path.open("rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise InstallError("the manifest is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError("the manifest is not UTF-8") from None


def _venv_program(
    venv: Path, argv: Sequence[str], *, installed: bool = True,
) -> tuple[str, ...]:
    """Point a manifest command at the plugin's venv; refuse anything else."""
    program = argv[0]
    if program in _PYTHONS:
        return (str(venv / "bin" / "python"), *argv[1:])
    if "/" in program or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", program):
        raise InstallError(
            "Exec and Cli must name a program the plugin installs (or python), "
            f"not {program[:60]!r}"
        )
    path = venv / "bin" / program
    if installed and not path.exists():
        raise InstallError(f"the plugin did not install the program {program}")
    return (str(path), *argv[1:])


def _rewrite(text: str, exec_argv: Sequence[str], cli_argv: Sequence[str]) -> str:
    lines, group = [], None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
        elif group == "BlueFerry Plugin":
            key = stripped.partition("=")[0].strip()
            if key == "Exec":
                raw = "Exec=" + shlex.join(exec_argv)
            elif key == "Cli" and cli_argv:
                raw = "Cli=" + shlex.join(cli_argv)
        lines.append(raw)
    return "\n".join(lines) + "\n"


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.loads(stream.read(1024 * 1024))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def _write_json(path: Path, value: object, *, private: bool) -> None:
    _write_text(path, json.dumps(value, indent=2) + "\n", 0o600 if private else 0o644)
