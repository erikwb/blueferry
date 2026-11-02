"""The phone card: a strip above the tabs with the iPhone's name, connection,
battery/signal/network and the opt-in now playing.

GTK counterpart of the Qt client's ``PhoneCard.qml``. The card also reads
the daemon status for the Calls and Notifications tabs (see
:meth:`PhoneCard.add_status_listener`), so the window asks once per
``StatusChanged``. Nothing is read while the window is hidden
(:meth:`PhoneCard.set_watched`). Every label takes iPhone text (the phone's
name, track titles, the operator) verbatim, never as Pango markup. The
Quick Settings button holds the sound and away lock switches
(``ui/quick_switches.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from gi.repository import GLib, Gtk, Pango

from blueferry import phone_link_view
from blueferry import phone_overview as overview
from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.ui.phone_presenter import media, phone_status
from blueferry.ui.quick_switches import (
    PhoneAudioSwitch,
    ProximitySwitch,
    away_lock_row,
    sound_row,
)
from blueferry.ui.reads import CoalescedRead

_END = Pango.EllipsizeMode.END
NameLoader = Callable[[Callable[[str], None]], None]


def _media_buttons() -> dict[str, tuple[str, str]]:
    return {
        "previous": ("media-skip-backward-symbolic", _("Previous Track")),
        "toggle": ("media-playback-start-symbolic", _("Play or Pause")),
        "next": ("media-skip-forward-symbolic", _("Next Track")),
    }


def load_phone_name(done: Callable[[str], None]) -> None:
    """Read the configured iPhone's Bluetooth name off the GTK thread."""
    from blueferry.setup_client import SetupClient

    def work() -> None:
        try:
            setup = SetupClient()
            mac = setup.configuration().mac
            devices = [device.to_dict() for device in setup.devices()]
            name = phone_link_view.phone_name(devices, mac)
        except Exception:
            name = "iPhone"

        def deliver() -> bool:
            done(name)
            return False

        GLib.idle_add(deliver)

    threading.Thread(target=work, name="blueferry-phone-name", daemon=True).start()


def _hint_label() -> Gtk.Label:
    return Gtk.Label(
        xalign=0, wrap=True, lines=2, ellipsize=_END,
        max_width_chars=48, use_markup=False, css_classes=["dim-label", "caption"],
        hexpand=True,
    )


class PhoneCard(Gtk.Box):
    def __init__(self, client, toast, *, name_loader: NameLoader = load_phone_name) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
        )
        self._client = client
        self._toast = toast
        self._name_loader = name_loader
        self._watched = False
        self._status: dict = BackendStatus().to_dict()
        self._now_playing: dict = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._status_read = CoalescedRead(
            client.get_status_async, self._status_loaded,
            lambda _message: self._status_loaded(BackendStatus()),
            active=lambda: self._watched,
        )
        self._media_read = CoalescedRead(
            client.now_playing_async, self._media_loaded,
            lambda _message: self._media_loaded({}),
            active=lambda: self._watched and overview.media_control_enabled(self._status),
        )

        # ---- identity: icon, name, connection ---------------------------
        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        identity.append(Gtk.Image(
            icon_name="phone-apple-iphone-symbolic", pixel_size=32, valign=Gtk.Align.CENTER,
        ))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.name_label = Gtk.Label(
            label="iPhone", xalign=0, ellipsize=_END, max_width_chars=24,
            use_markup=False, css_classes=["heading"],
        )
        self.connection_label = Gtk.Label(
            xalign=0, ellipsize=_END, max_width_chars=28, use_markup=False,
            css_classes=["dim-label", "caption"],
        )
        names.append(self.name_label)
        names.append(self.connection_label)
        identity.append(names)
        self.append(identity)

        # ---- battery, signal and network, or why they are missing -------
        self.status_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            valign=Gtk.Align.CENTER, hexpand=True,
        )
        self.status_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.status_hint = _hint_label()
        self.status_box.append(self.status_chips)
        self.status_box.append(self.status_hint)
        self.append(self.status_box)

        # ---- now playing, or why it is not shown ------------------------
        self.media_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            valign=Gtk.Align.CENTER, hexpand=True,
        )
        self.media_box.update_property([Gtk.AccessibleProperty.LABEL], [_("Now Playing")])
        self.media_box.append(Gtk.Image(icon_name="audio-x-generic-symbolic"))
        self.media_title = Gtk.Label(
            xalign=0, ellipsize=_END, hexpand=True, max_width_chars=36, use_markup=False,
        )
        self.media_box.append(self.media_title)
        self.media_buttons: dict[str, Gtk.Button] = {}
        for command, (icon, label) in _media_buttons().items():
            button = Gtk.Button(
                icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=label,
                css_classes=["flat", "circular"],
            )
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("clicked", lambda _b, name=command: self._media_command(name))
            self.media_box.append(button)
            self.media_buttons[command] = button
        self.media_hint = _hint_label()
        self.media_box.append(self.media_hint)
        self.append(self.media_box)

        # ---- quick settings: the sound and the away lock ----------------
        switches = Gtk.ListBox(
            css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
            width_request=320,
        )
        self.sound = PhoneAudioSwitch(sound_row(), client, toast)
        self.away_lock = ProximitySwitch(away_lock_row(), client, toast)
        switches.append(self.sound.row)
        switches.append(self.away_lock.row)
        self.quick_settings = Gtk.MenuButton(
            icon_name="preferences-system-symbolic", tooltip_text=_("Quick Settings"),
            valign=Gtk.Align.CENTER, halign=Gtk.Align.END, css_classes=["flat"],
            popover=Gtk.Popover(child=switches),
        )
        self.quick_settings.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Quick Settings")],
        )
        self.append(self.quick_settings)

        client.connect("status-invalidated", lambda _c: self._status_read.refresh())
        client.connect("availability-changed", self._availability_changed)
        client.connect("now-playing-changed", lambda _c: self._media_read.refresh())
        self._render()
        self._name_loader(self._name_loaded)

    # ---- the window's side ---------------------------------------------

    @property
    def status(self) -> dict:
        return self._status

    def add_status_listener(self, listener: Callable[[dict], None]) -> None:
        """Call ``listener(status)`` with every status read (``to_dict``)."""
        self._listeners.append(listener)
        listener(self._status)

    def set_watched(self, watched: bool) -> None:
        """Read only while the window is shown; forget now playing when hidden."""
        if watched == self._watched:
            return
        self._watched = watched
        self._status_read.reset()
        self._media_read.reset()
        if watched:
            self._status_read.refresh()
            self._media_read.refresh()
        else:
            self._now_playing = {}
            self._render_media()

    def refresh(self) -> None:
        self._status_read.refresh()

    # ---- loading --------------------------------------------------------

    def _availability_changed(self, _client, available: bool) -> None:
        self._status_read.refresh()
        if available:
            self._name_loader(self._name_loaded)

    def _name_loaded(self, name: str) -> None:
        self.name_label.set_label(name or "iPhone")

    def _status_loaded(self, status: BackendStatus) -> None:
        values = status.to_dict()
        if overview.media_control_enabled(values) != overview.media_control_enabled(
            self._status
        ):
            self._media_read.reset()
            self._now_playing = {}
        self._status = values
        self._render()
        self._media_read.refresh()
        for listener in self._listeners:
            listener(values)

    def _media_loaded(self, snapshot: object) -> None:
        self._now_playing = snapshot if isinstance(snapshot, dict) else {}
        self._render_media()

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        self.connection_label.set_label(phone_link_view.connection_text(self._status))
        self.sound.apply(self._status)
        self.away_lock.apply(self._status)
        state = phone_status(self._status)
        child = self.status_chips.get_first_child()
        while child is not None:
            self.status_chips.remove(child)
            child = self.status_chips.get_first_child()
        for chip in state.chips:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            item.append(Gtk.Image(icon_name=chip.icon))
            item.append(Gtk.Label(label=chip.text, use_markup=False, ellipsize=_END))
            self.status_chips.append(item)
        self.status_chips.set_visible(bool(state.chips))
        self.status_chips.set_tooltip_text(state.summary or None)
        self.status_chips.update_property([Gtk.AccessibleProperty.LABEL], [state.summary])
        self.status_hint.set_label(state.hint)
        self.status_hint.set_tooltip_text(state.hint or None)
        self.status_hint.set_visible(not state.chips and bool(state.hint))
        self._render_media()

    def _render_media(self) -> None:
        state = media(self._status, self._now_playing)
        self.media_title.set_visible(state.available)
        self.media_title.set_label(state.title)
        self.media_title.set_tooltip_text(state.title or None)
        for name, button in self.media_buttons.items():
            button.set_visible(state.available)
            button.set_sensitive(bool(state.buttons.get(name)))
        self.media_buttons["toggle"].set_icon_name(
            "media-playback-pause-symbolic" if state.playing
            else "media-playback-start-symbolic"
        )
        self.media_hint.set_visible(not state.available)
        self.media_hint.set_label(state.hint)
        self.media_hint.set_tooltip_text(state.hint or None)

    # ---- actions --------------------------------------------------------

    def _media_command(self, command: str) -> None:
        def failed(message: str) -> None:
            self._toast(message)
            self._media_read.refresh()

        self._client.send_media_command_async(command, lambda _value: None, failed)
