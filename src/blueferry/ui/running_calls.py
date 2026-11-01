"""Running phone calls on the Calls tab: answer, hold or resume, hang up.

GTK counterpart of the call cards in the Qt client's ``CallsTab.qml``. The
calls come from ``Calls1.ListCalls``, read again on ``CallsChanged``, when
the phone, daemon availability or call readiness changes and after each
action. They are not private history, so, as in Qt, they are read
whichever tab is visible; unlike Qt, which reads regardless of
visibility, the GTK client deliberately reads only while the window is
shown. The duration ticks only while the tab is visible. The wording is
``phone_link_view.call_card``; a failed ``ListCalls`` reports the calls as
unavailable.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gi.repository import Adw, Gtk

from blueferry import phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.models import CallsSnapshot
from blueferry.ui.avatars import AvatarCache
from blueferry.ui.quick_switches import PhoneAudioSwitch, sound_row
from blueferry.ui.reads import CoalescedRead
from blueferry.ui.util import glib_timer

TICK_MS = 1000


class RunningCalls(Gtk.ListBox):
    def __init__(
        self,
        client,
        toast: Callable[[str], None],
        *,
        on_state: Callable[[], None] = lambda: None,
        timer: TimerFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
            margin_top=12, margin_start=12, margin_end=12, visible=False,
        )
        self._client = client
        self._toast = toast
        self._on_state = on_state
        self._clock = clock
        self._status: dict = {}
        self._status_key: tuple | None = None
        self._shown = False
        self._watched = False
        self._busy = False
        self._generation = 0
        self._rows_generation = 0
        self._ticking = False
        self.calls: list[dict] = []
        self._state: str | None = None
        self.rows: list[tuple[Adw.ActionRow, dict, Adw.Avatar, list[Gtk.Button]]] = []
        self._read = CoalescedRead(
            client.calls_async, self._loaded, self._failed,
            active=self._available,
        )
        self._ticker = (timer or glib_timer)(self._tick)
        self._avatars = AvatarCache(client.contact_photo_async, self._refresh_avatars)
        # Where the sound plays right now, as on the phone card.
        self.sound = PhoneAudioSwitch(sound_row(), client, toast)
        client.connect("calls-changed", lambda _c: self._read.refresh())

    # ---- the page's side -----------------------------------------------

    @property
    def state(self) -> str:
        """The Calls1 state: from ListCalls once read, else from the status."""
        if self._state is not None:
            return self._state
        return str(self._status.get("calls_state") or "disabled")

    def apply_status(self, status: dict) -> None:
        self._status = status
        self.sound.apply(status)
        key = tuple(status.get(field) for field in (
            "daemon", "calls_enabled", "calls_state", "phone_profile_id",
        ))
        if key == self._status_key:
            self._avatars.update_status(status)
            return
        self._status_key = key
        self._forget_calls()
        self._avatars.update_status(status)
        self._read.refresh()

    def set_shown(self, shown: bool) -> None:
        """Read while the window is shown."""
        if shown == self._shown:
            return
        self._shown = shown
        if shown:
            self._avatars.update_status(self._status)
            self._read.refresh()
        else:
            self._forget_calls()
        self._sync_ticker()

    def set_watched(self, watched: bool) -> None:
        """Tick the durations while the Calls tab is visible."""
        self._watched = watched
        self._sync_ticker()

    # ---- loading --------------------------------------------------------

    def _available(self) -> bool:
        return (self._shown and self._status.get("daemon") is True
                and self._status.get("calls_enabled") is True)

    def _forget_calls(self) -> None:
        self._generation += 1
        self._busy = False
        self._state = None
        self._read.reset()
        self._set_calls([])
        self._avatars.update_status({})

    def _loaded(self, snapshot: object) -> None:
        if not isinstance(snapshot, CallsSnapshot):
            return
        self._state = snapshot.state
        self._set_calls([call.to_dict() for call in snapshot.calls])

    def _failed(self, _message: str) -> None:
        # No stale calls or a stale "ready" after ListCalls failed.
        state = str(self._status.get("calls_state") or "disabled")
        self._state = "unavailable" if state == "ready" else state
        self._set_calls([])

    def _set_calls(self, calls: list[dict]) -> None:
        self._rows_generation += 1
        self.calls = calls
        self._render()
        self._on_state()

    # ---- actions --------------------------------------------------------

    def answer(self, call_id: str) -> None:
        self._act(lambda done, failed: self._client.answer_call_async(call_id, done, failed))

    def swap(self) -> None:
        self._act(self._client.swap_calls_async)

    def hangup(self, call_id: str) -> None:
        self._act(lambda done, failed: self._client.hangup_call_async(call_id, done, failed))

    def _act(self, request) -> None:
        if self._busy or not self._available() or self.state != "ready" or not self.calls:
            return
        generation = self._generation
        self._busy = True
        self._render_sensitivity()

        def finished() -> None:
            if generation != self._generation:
                return
            self._busy = False
            self._render_sensitivity()
            self._read.refresh()

        def failed(message: str) -> None:
            if generation != self._generation:
                return
            self._toast(message)
            finished()

        request(lambda _value: finished(), failed)

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        self.remove_all()
        self.rows = []
        now = self._clock()
        for call in self.calls:
            self.append(self._row(call, phone_link_view.call_card(call, now)))
        if self.calls:
            self.append(self.sound.row)
        self.set_visible(bool(self.calls))
        self._refresh_avatars()
        self._render_sensitivity()
        self._sync_ticker()

    def _row(self, call: dict, card: dict) -> Adw.ActionRow:
        row = Adw.ActionRow(title=card["title"], subtitle=card["subtitle"], use_markup=False)
        avatar = Adw.Avatar(size=36, text=card["title"], show_initials=True)
        row.add_prefix(avatar)
        call_id = str(call.get("call_id") or "")
        buttons = []
        if card["ringing"]:
            buttons.append(self._button(
                "call-start-symbolic", card["answer"], "suggested-action",
                lambda: self.answer(call_id),
            ))
        else:
            buttons.append(self._button(
                "media-playback-start-symbolic" if card["held"]
                else "media-playback-pause-symbolic",
                card["hold"], "flat", self.swap,
            ))
        buttons.append(self._button(
            "call-stop-symbolic", card["hangup"], "destructive-action",
            lambda: self.hangup(call_id),
        ))
        for button in buttons:
            row.add_suffix(button)
        self.rows.append((row, call, avatar, buttons))
        return row

    def _button(self, icon: str, label: str, style: str, action) -> Gtk.Button:
        button = Gtk.Button(
            icon_name=icon, tooltip_text=label, valign=Gtk.Align.CENTER,
            css_classes=["circular", style],
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        generation = self._rows_generation

        def clicked(_button) -> None:
            if generation == self._rows_generation:
                action()

        button.connect("clicked", clicked)
        return button

    def _render_sensitivity(self) -> None:
        for _row, _call, _avatar, buttons in self.rows:
            for button in buttons:
                button.set_sensitive(not self._busy and self._available() and self.state == "ready")

    def _refresh_avatars(self) -> None:
        if not self._shown:
            return
        for _row, call, avatar, _buttons in self.rows:
            avatar.set_custom_image(self._avatars.get(str(call.get("number") or "")))

    def _tick(self) -> None:
        self._ticking = False
        now = self._clock()
        for row, call, _avatar, _buttons in self.rows:
            row.set_subtitle(phone_link_view.call_card(call, now)["subtitle"])
        self._sync_ticker()

    def _sync_ticker(self) -> None:
        wanted = bool(self.calls) and self._shown and self._watched
        if wanted and not self._ticking:
            self._ticking = True
            self._ticker.start(TICK_MS)
        elif not wanted and self._ticking:
            self._ticking = False
            self._ticker.stop()
