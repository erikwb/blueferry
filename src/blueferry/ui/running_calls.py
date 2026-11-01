"""Running phone calls on the Calls tab: answer, hold or resume, hang up,
and keypad tones (DTMF) for the active call.

GTK counterpart of the call cards in the Qt client's ``CallsTab.qml``. The
calls come from ``Calls1.ListCalls``, read again on ``CallsChanged``, when
the status reports another ``(calls_enabled, calls_state)`` and after each
action. They are not private history, so, as in Qt, they are read
whichever tab is visible; unlike Qt, which reads regardless of
visibility, the GTK client deliberately reads only while the window is
shown. The duration ticks only while the tab is visible. The wording is
``phone_link_view.call_card``; a failed ``ListCalls`` reports the calls as
unavailable. The rows are rebuilt on each read, so the keypad-tone text
is kept here and put back into the new row.
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
        self._ticking = False
        self.calls: list[dict] = []
        self._state: str | None = None
        self.rows: list[tuple[Adw.ActionRow, dict, Adw.Avatar, list[Gtk.Button]]] = []
        # Keypad tones for the active call: the row, its send button and
        # the text typed so far (kept across re-renders).
        self.tones: Adw.EntryRow | None = None
        self.tones_button: Gtk.Button | None = None
        self._tones_call = ""
        self._tones_draft = ""
        # Keypad, keyboard and tone field all go through one ordered queue
        # (phone_link_view.KeypadTones, shared with Qt and the terminal).
        self.keypad = phone_link_view.KeypadTones(
            self._send_tones, on_changed=self._render_tones_sent, on_error=toast,
        )
        self.tones_sent = Adw.ActionRow(use_markup=False, css_classes=["dim-label"])
        self._read = CoalescedRead(
            client.calls_async, self._loaded, self._failed,
            active=lambda: self._shown and self._status.get("calls_enabled") is True,
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
        self._avatars.update_status(status)
        key = (status.get("calls_enabled"), status.get("calls_state"))
        if key == self._status_key:
            return
        self._status_key = key
        self._state = None
        self._read.reset()
        if status.get("calls_enabled") is True:
            self._read.refresh()
        else:
            self._set_calls([])

    def set_shown(self, shown: bool) -> None:
        """Read while the window is shown."""
        if shown == self._shown:
            return
        self._shown = shown
        if shown:
            self._read.refresh()
        else:
            self._read.reset()
        self._sync_ticker()

    def set_watched(self, watched: bool) -> None:
        """Tick the durations while the Calls tab is visible."""
        self._watched = watched
        self._sync_ticker()

    # ---- loading --------------------------------------------------------

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

    @property
    def active_call_id(self) -> str:
        """The call keypad tones go to; "" while no call is connected."""
        return phone_link_view.active_call_id(self.calls)

    def press_tone(self, key: str) -> bool:
        """A keypad click or key press during an active call: send it now."""
        return self.keypad.press(self.active_call_id, key)

    def send_tones(self) -> None:
        """Send the typed keypad tones to the active call, then clear them."""
        if self.keypad.press(self._tones_call, self._tones_draft):
            self._tones_draft = ""
            if self.tones is not None:
                self.tones.set_text("")

    def _send_tones(self, call_id: str, tones: str, done, failed) -> None:
        self._client.send_call_tones_async(call_id, tones, done, failed)

    def _render_tones_sent(self) -> None:
        text = phone_link_view.tones_feedback(self.keypad.sent)
        self.tones_sent.set_title(text)
        self.tones_sent.set_visible(bool(text))

    def _act(self, request) -> None:
        if self._busy:
            return
        self._busy = True
        self._render_sensitivity()

        def finished() -> None:
            self._busy = False
            self._render_sensitivity()
            self._read.refresh()

        def failed(message: str) -> None:
            self._toast(message)
            finished()

        request(lambda _value: finished(), failed)

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        typing = self.tones is not None and focus is not None and focus.is_ancestor(self.tones)
        cursor = self.tones.get_position() if typing and self.tones is not None else -1
        self.remove_all()
        self.rows = []
        self.tones = self.tones_button = None
        self._tones_call = ""
        now = self._clock()
        for call in self.calls:
            card = phone_link_view.call_card(call, now)
            self.append(self._row(call, card))
            if card["active"] and self.tones is None:
                self._tones_call = str(call.get("call_id") or "")
                self.append(self._tones_row(card))
                self.append(self.tones_sent)
        self.keypad.follow(self._tones_call)
        self._render_tones_sent()
        if self.tones is None:
            self._tones_draft = ""  # no active call any more
        elif typing:
            self.tones.grab_focus()
            self.tones.set_position(cursor if 0 <= cursor <= len(self._tones_draft) else -1)
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

    def _tones_row(self, card: dict) -> Adw.EntryRow:
        # Adw.EntryRow:max-length is libadwaita 1.6; dial_tones caps the text.
        row = Adw.EntryRow(
            title=card["tones"], use_markup=False, input_purpose=Gtk.InputPurpose.PHONE,
        )
        row.set_text(self._tones_draft)
        row.connect("changed", self._tones_changed)
        row.connect("entry-activated", lambda _row: self.send_tones())
        button = Gtk.Button(
            icon_name="input-dialpad-symbolic", tooltip_text=card["send_tones"],
            valign=Gtk.Align.CENTER, css_classes=["flat"],
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [card["send_tones"]])
        button.connect("clicked", lambda _b: self.send_tones())
        row.add_suffix(button)
        self.tones, self.tones_button = row, button
        return row

    def _tones_changed(self, row: Adw.EntryRow) -> None:
        text = row.get_text()
        cleaned = phone_link_view.dial_tones(text)
        if cleaned != text:
            # Keep the cursor where it was, minus the characters dropped before it.
            position = len(phone_link_view.dial_tones(text[:row.get_position()]))
            row.set_text(cleaned)  # runs this handler again with the clean text
            row.set_position(position)
            return
        self._tones_draft = cleaned
        self._render_sensitivity()

    @staticmethod
    def _button(icon: str, label: str, style: str, action) -> Gtk.Button:
        button = Gtk.Button(
            icon_name=icon, tooltip_text=label, valign=Gtk.Align.CENTER,
            css_classes=["circular", style],
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        button.connect("clicked", lambda _b: action())
        return button

    def _render_sensitivity(self) -> None:
        for _row, _call, _avatar, buttons in self.rows:
            for button in buttons:
                button.set_sensitive(not self._busy)
        if self.tones_button is not None:
            # Tones queue up on their own; other actions do not block them.
            self.tones_button.set_sensitive(bool(self._tones_draft))

    def _refresh_avatars(self) -> None:
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
