"""Discover the iPhone's oFono HFP modem, bring it online, and track its calls.

Portions of the discovery ranking, the Powered-then-Online bring-up, the
retry schedule, and the CallVolume adjustment are derived from tincan's
``tincand/call_controller.py`` and ``tincand/call_audio.py``
(https://github.com/quad341/tincan), used under the MIT License:

    Copyright (c) 2026 quad341

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
    NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
    USE OR OTHER DEALINGS IN THE SOFTWARE.

The BlueFerry adaptation is asynchronous throughout (no synchronous oFono
call on the GLib loop), is bound to the configured iPhone MAC, and fails
closed when oFono is absent. BlueFerry as a whole is GPL-2.0-or-later.

iOS/oFono quirk: the iPhone's hands-free modem does not power itself up. The
controller sets ``Modem.Powered=true`` (oFono then connects the HFP profile
and establishes the service-level connection), waits for oFono to report
``Powered=true``, and only then requests ``Online=true``; oFono rejects Online
before Powered. oFono's HFP driver registers ``VoiceCallManager`` already
while powering the modem (``hfp_pre_sim``), so its presence in ``Interfaces``
alone does not mean call control works: the controller treats the modem as
voice-ready only when it is Online *and* lists ``VoiceCallManager``.

Phone status: oFono creates ``Handsfree`` and ``NetworkRegistration`` in the
same ``hfp_pre_sim`` step, and they survive Online dropping. The controller
therefore watches them, when oFono lists them, from ``Powered=true`` on (see
``phone_status``), so the battery stays visible even when the Online step is
stuck in the oFono/WirePlumber profile race. They are strictly
observational; a missing interface only leaves those values unknown.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from gi.repository import GLib

from blueferry.calls.model import (
    CALL_VOLUME_IFACE,
    CALLS_BLUEZ_CONFLICT,
    CALLS_CONNECTING,
    CALLS_DISABLED,
    CALLS_READY,
    CALLS_SEARCHING,
    CALLS_UNAVAILABLE,
    MANAGER_IFACE,
    MAX_TRACKED_CALLS,
    MODEM_IFACE,
    VOICE_CALL_IFACE,
    VOICE_CALL_MANAGER_IFACE,
    CallEvent,
    CallEventKind,
    CallRecord,
    ModemInfo,
    call_id_from_path,
    normalize_dial_number,
    parse_call,
    parse_modem,
    remote_text,
    select_modem,
    validate_call_id,
    validate_dtmf,
)
from blueferry.calls.ofono import (
    DBusOfonoTransport,
    OfonoTransport,
    SignalMatch,
    access_denied,
    boolean,
    byte,
    interface_missing,
    public_error,
    service_missing,
)
from blueferry.calls.phone_status import (
    HANDSFREE_IFACE,
    PHONE_STATUS_IFACES,
    PhoneStatus,
    apply_properties,
)
from blueferry.errors import (
    CALLS_DISABLED_HINT,
    CallsDisabledError,
    CallsUnavailableError,
    InvalidArgumentsError,
    NotFoundError,
    OperationFailedError,
)

log = logging.getLogger(__name__)

# Fast backoff while the modem is expected shortly (1+2+4+8+15 s), then a
# steady cadence so a phone that stays away overnight reconnects without a
# daemon restart. Adapted from tincan's _RETRY_STEPS/_RETRY_STEADY_STATE_S.
RETRY_STEPS_SEC = (1, 2, 4, 8, 15)
RETRY_STEADY_SEC = 30
# oFono missing is not expected to fix itself quickly; its owner watch
# rediscovers immediately when it starts, so this is only a safety net.
UNAVAILABLE_RETRY_SEC = 60
# Powered waits for the HFP service-level connection. If neither Powered nor
# Online arrives in this window, retry the bring-up through the backoff.
BRINGUP_TIMEOUT_SEC = 30
# One failed Powered proves nothing (the phone may be busy or just out of
# range). Only this many failures in a row, with bluetoothd's own HFP plugin
# loaded, are reported as a BlueZ conflict; paging then slows down to this
# cadence so the state still recovers without a Classic reconnect.
POWER_ATTEMPTS_BEFORE_CONFLICT = 3
CONFLICT_RETRY_SEC = 300
# oFono's CallVolume is a percentage; iPhone call audio is inaudibly faint at
# oFono's 50 % default.
CALL_VOLUME_MAX = 100

Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Success = Callable[[Any], None]
Failure = Callable[[Exception], None]


class CallController:
    """Own oFono discovery, modem bring-up, call state, and call control.

    Every oFono interaction goes through the injected transport and every
    timer through ``schedule``/``cancel``, so the controller runs unchanged
    against inert fakes. Replies and signals carry the generation in which
    they were requested; a reset (oFono restart, modem removal, stop)
    invalidates everything still in flight.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        mac: str,
        adapter: str,
        transport: OfonoTransport | None = None,
        resolve_contact: Callable[[str], str | None] | None = None,
        on_calls_changed: Callable[[], None] | None = None,
        on_state_changed: Callable[[], None] | None = None,
        on_event: Callable[[CallEvent], None] | None = None,
        phone_reachable: Callable[[], bool] | None = None,
        hfp_conflict: Callable[[], bool] | None = None,
        on_phone_status: Callable[[PhoneStatus], None] | None = None,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
    ) -> None:
        self.enabled = bool(enabled)
        self._mac = mac
        self._adapter = adapter
        self._transport: OfonoTransport | None = transport
        self._resolve_contact = resolve_contact or (lambda _number: None)
        self._on_calls_changed = on_calls_changed or (lambda: None)
        self._on_state_changed = on_state_changed or (lambda: None)
        self._on_event = on_event or (lambda _event: None)
        self._phone_reachable = phone_reachable or (lambda: True)
        self._hfp_conflict = hfp_conflict or (lambda: False)
        self._on_phone_status = on_phone_status or (lambda _status: None)
        self._schedule = schedule
        self._cancel = cancel

        self._state = CALLS_DISABLED if not self.enabled else CALLS_UNAVAILABLE
        self._running = False
        self._generation = 0
        self._calls_generation = 0
        self._owner_match: SignalMatch | None = None
        self._manager_matches: list[SignalMatch] = []
        self._modem: ModemInfo | None = None
        self._modem_match: SignalMatch | None = None
        self._vcm_matches: list[SignalMatch] = []
        self._call_matches: dict[str, SignalMatch] = {}
        self._calls: dict[str, CallRecord] = {}
        self._snapshot_changes: set[str] | None = None
        self._bound_path: str | None = None
        self._retry_id: int | None = None
        self._retry_index = 0
        self._bringup_id: int | None = None
        self._request_in_flight = False
        # Modem properties oFono accepted for the current bring-up attempt.
        # Its PropertyChanged confirmation may lag the method reply.
        self._requested: set[str] = set()
        # The iPhone modem BlueFerry powered up, or found powered while calls
        # were on (its own earlier process may have crashed). It is powered
        # down again on stop; a modem never seen while enabled is left alone.
        self._powered_path: str | None = None
        self._discovering = False
        # Powered attempts that failed in a row, and whether they were
        # reported as a BlueZ HFP conflict. Both are cleared when Classic
        # returns or oFono makes progress.
        self._power_failures = 0
        self._blocked = False
        self._last_reachable = False
        # Log each distinct discovery failure once, not on every retry.
        self._last_discovery_error = ""
        # Phone battery/signal/operator from Handsfree and NetworkRegistration.
        self._phone = PhoneStatus()
        self._phone_path: str | None = None
        self._phone_matches: dict[str, SignalMatch] = {}
        # A fresh token per watched interface; replies to a superseded
        # GetProperties request are dropped.
        self._phone_tokens: dict[str, object] = {}
        # Pending one-shot GetProperties retries, per interface.
        self._phone_retries: dict[str, int] = {}
        # Log-once keys, reset whenever the phone status is dropped.
        self._phone_errors: set[str] = set()
        self._phone_announced = False

    # ---- public state -------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def available(self) -> bool:
        return self._state == CALLS_READY

    def calls(self) -> list[CallRecord]:
        return list(self._calls.values())

    @property
    def in_call(self) -> bool:
        """Whether the phone reports any call (ringing, dialing, active, held)."""
        return bool(self._calls)

    @property
    def phone_status(self) -> PhoneStatus:
        return self._phone

    def snapshot(self) -> dict[str, object]:
        """Status fields merged into the unicast GetStatus reply.

        While calls are off only ``calls_enabled`` is reported: clients use
        its presence to offer the opt-in, and nothing else about calls is
        visible.
        """
        if not self.enabled:
            return {"calls_enabled": False}
        return {
            "calls_enabled": self.enabled,
            "calls_state": self._state,
            "calls_available": self.available,
        }

    def list_calls(self) -> dict[str, object]:
        """Unicast ListCalls payload. Contains caller numbers and names."""
        if not self.enabled:
            raise CallsDisabledError(CALLS_DISABLED_HINT)
        return {
            "state": self._state,
            "calls": [record.to_wire() for record in self._calls.values()],
        }

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._generation += 1
        try:
            transport = self._ensure_transport()
            self._owner_match = transport.watch_owner(self._on_owner_changed)
            self._watch_manager(transport)
        except Exception:
            # The system bus itself can be missing in minimal sessions. The
            # messaging daemon must keep running; try again later.
            log.warning("could not watch oFono on the system bus; calls unavailable", exc_info=True)
            self._remove_watches()
            self._set_state(CALLS_UNAVAILABLE)
            self._running = False
            self._schedule_restart()
            return
        log.info("phone calls enabled; looking for the iPhone's oFono HFP modem")
        self._discover()

    def stop(self) -> None:
        self._running = False
        self._blocked = False
        self._generation += 1
        self._discovering = False
        self._cancel_timer("_retry_id")
        self._cancel_timer("_bringup_id")
        self._unbind(emit=False)
        self._power_down()
        self._release_modem()
        self._remove_watches()

    def set_enabled(self, enabled: bool) -> None:
        """Switch the feature on or off at runtime (the saved opt-in changed)."""
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        if enabled:
            self.enabled = True
            self._last_discovery_error = ""
            self._set_state(CALLS_UNAVAILABLE)
            self.start()
            return
        # Switching off ends the desktop's view of any call (popups close)
        # and releases the hands-free link; the call itself stays on the
        # phone.
        self._unbind(emit=True)
        self.stop()
        self.enabled = False
        self._set_state(CALLS_DISABLED)

    def poke(self) -> None:
        """Retry bring-up promptly when the phone's Classic link returns."""
        reachable = self._reachable()
        returned = reachable and not self._last_reachable
        self._last_reachable = reachable
        if (
            returned and self._running and self._modem is not None
            and self._state in (CALLS_CONNECTING, CALLS_BLUEZ_CONFLICT)
            and not self._request_in_flight
        ):
            # A fresh Classic link is worth a new series of attempts, also
            # after a BlueZ HFP conflict (the user may have restarted
            # bluetoothd).
            self._blocked = False
            self._power_failures = 0
            self._retry_index = 0
            self._cancel_timer("_retry_id")
            self._advance()

    # ---- internals: plumbing -------------------------------------------

    def _ensure_transport(self) -> OfonoTransport:
        if self._transport is None:
            self._transport = DBusOfonoTransport()
        return self._transport

    def _reachable(self) -> bool:
        try:
            return bool(self._phone_reachable())
        except Exception:
            log.debug("phone reachability check failed", exc_info=True)
            return False

    def _guard(
        self, handler: Callable[..., None], *, binding: bool = False,
    ) -> Callable[..., None]:
        """Drop signals delivered after a reset or stop."""
        generation = self._generation
        calls_generation = self._calls_generation

        def guarded(*args: Any) -> None:
            if not self._running or generation != self._generation:
                return
            if binding and calls_generation != self._calls_generation:
                return
            try:
                handler(*args)
            except Exception:
                log.exception("unexpected oFono signal handling failure")

        return guarded

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: tuple[Any, ...],
        on_reply: Callable[..., None],
        on_error: Failure,
        on_stale_reply: Callable[[], None] | None = None,
    ) -> None:
        generation = self._generation

        def replied(*values: Any) -> None:
            if generation == self._generation:
                on_reply(*values)
            elif on_stale_reply is not None:
                on_stale_reply()

        def failed(error: Exception) -> None:
            if generation == self._generation:
                on_error(error)

        try:
            self._ensure_transport().call(
                path, interface, method, signature, args, replied, failed,
            )
        except Exception as error:
            # dbus-python can raise synchronously, e.g. when the system bus
            # connection itself is gone. Report it like an asynchronous error.
            failed(error)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        log.info("phone calls: %s -> %s", self._state, state)
        self._state = state
        try:
            self._on_state_changed()
        except Exception:
            log.exception("calls state callback failed")

    def _calls_changed(self) -> None:
        try:
            self._on_calls_changed()
        except Exception:
            log.exception("calls change callback failed")

    def _emit(self, kind: CallEventKind, record: CallRecord) -> None:
        try:
            self._on_event(CallEvent(kind=kind, call=record))
        except Exception:
            log.exception("call event consumer failed")

    def _cancel_timer(self, attribute: str) -> None:
        source = getattr(self, attribute)
        if source is None:
            return
        setattr(self, attribute, None)
        try:
            self._cancel(source)
        except Exception:
            log.debug("could not remove calls timer", exc_info=True)

    @staticmethod
    def _remove(match: SignalMatch | None) -> None:
        if match is None:
            return
        try:
            match.remove()
        except Exception:
            log.debug("could not remove oFono signal watch", exc_info=True)

    def _remove_watches(self) -> None:
        self._remove(self._owner_match)
        self._owner_match = None
        for match in self._manager_matches:
            self._remove(match)
        self._manager_matches = []

    # ---- internals: retry --------------------------------------------

    def _next_delay(self) -> int:
        if self._retry_index < len(RETRY_STEPS_SEC):
            delay = RETRY_STEPS_SEC[self._retry_index]
        else:
            if self._retry_index == len(RETRY_STEPS_SEC):
                log.info(
                    "iPhone HFP modem still not usable; polling every %ds",
                    RETRY_STEADY_SEC,
                )
            delay = RETRY_STEADY_SEC
        self._retry_index += 1
        return delay

    def _schedule_retry(self, delay: int | None = None) -> None:
        if not self._running or self._retry_id is not None:
            return
        selected = self._next_delay() if delay is None else delay
        self._retry_id = self._schedule(selected, self._retry_tick)

    def _retry_tick(self) -> bool:
        self._retry_id = None
        if self._running:
            self._discover()
        return False

    def _schedule_restart(self) -> None:
        if self._retry_id is not None:
            return
        self._retry_id = self._schedule(UNAVAILABLE_RETRY_SEC, self._restart_tick)

    def _restart_tick(self) -> bool:
        self._retry_id = None
        self.start()
        return False

    # ---- internals: discovery -----------------------------------------

    def _watch_manager(self, transport: OfonoTransport) -> None:
        # Append each match as soon as it exists, so a failure on the second
        # watch still leaves the first one tracked for removal.
        for signal, handler in (
            ("ModemAdded", self._on_modem_added),
            ("ModemRemoved", self._on_modem_removed),
        ):
            self._manager_matches.append(transport.watch(
                self._guard(handler), interface=MANAGER_IFACE, signal=signal, path="/",
            ))

    def _on_owner_changed(self, present: bool) -> None:
        if not self._running:
            return
        # Everything bound to the previous oFono process is gone, including
        # the modem it powered for us.
        self._generation += 1
        self._powered_path = None
        self._blocked = False
        self._cancel_timer("_retry_id")
        self._cancel_timer("_bringup_id")
        self._unbind(emit=True)
        self._release_modem()
        self._discovering = False
        self._request_in_flight = False
        self._retry_index = 0
        # Manager watches are keyed to the well-known name, so dbus-python
        # follows the new owner; rebind their generation guards.
        for match in self._manager_matches:
            self._remove(match)
        self._manager_matches = []
        try:
            self._watch_manager(self._ensure_transport())
        except Exception:
            log.warning("could not re-watch oFono modems", exc_info=True)
        if present:
            log.info("oFono appeared on the system bus; looking for the iPhone modem")
            self._set_state(CALLS_SEARCHING)
            self._discover()
        else:
            log.info("oFono left the system bus; phone calls unavailable")
            self._set_state(CALLS_UNAVAILABLE)
            self._schedule_retry(UNAVAILABLE_RETRY_SEC)

    def _discover(self) -> None:
        if not self._running or self._discovering:
            return
        self._discovering = True
        self._call(
            "/", MANAGER_IFACE, "GetModems", "", (),
            self._on_modems, self._on_modems_failed,
        )

    def _on_modems(self, modems: object = ()) -> None:
        self._discovering = False
        self._last_discovery_error = ""
        parsed = [
            modem
            for entry in (modems if isinstance(modems, list | tuple) else ())
            if isinstance(entry, list | tuple) and len(entry) == 2
            and (modem := parse_modem(entry[0], entry[1])) is not None
        ]
        selected = select_modem(parsed, mac=self._mac, adapter=self._adapter)
        if selected is None:
            if self._modem is not None:
                self._unbind(emit=True)
                self._release_modem()
            self._set_state(CALLS_SEARCHING)
            self._schedule_retry()
            return
        self._adopt(selected)

    def _on_modems_failed(self, error: Exception) -> None:
        self._discovering = False
        if access_denied(error):
            if self._last_discovery_error != "access-denied":
                log.info(
                    "oFono denies access; its shipped D-Bus policy ofono.conf (e.g. "
                    "/etc/dbus-1/system.d/ofono.conf or "
                    "/usr/share/dbus-1/system.d/ofono.conf) only allows root and "
                    "at_console — add a per-user or per-group allow rule"
                )
            self._last_discovery_error = "access-denied"
            self._set_state(CALLS_UNAVAILABLE)
            self._schedule_retry(UNAVAILABLE_RETRY_SEC)
            return
        if service_missing(error):
            if self._last_discovery_error != "service-missing":
                log.info("oFono is not running; phone calls unavailable")
            self._last_discovery_error = "service-missing"
            self._set_state(CALLS_UNAVAILABLE)
            self._schedule_retry(UNAVAILABLE_RETRY_SEC)
            return
        name = public_error(error)
        if self._last_discovery_error != name:
            log.warning("oFono GetModems failed: %s", name)
        self._last_discovery_error = name
        self._schedule_retry()

    def _on_modem_added(self, path: object, properties: object) -> None:
        modem = parse_modem(path, properties)
        if modem is None:
            return
        current = self._modem
        if (
            current is not None and current.path != modem.path
            and current.voice_ready and not modem.voice_ready
        ):
            # Never drop a working call link for a modem that is still offline.
            return
        candidate = select_modem(
            [modem] + ([current] if current is not None else []),
            mac=self._mac, adapter=self._adapter,
        )
        if candidate is not None and candidate.path == modem.path:
            self._cancel_timer("_retry_id")
            self._adopt(modem)

    def _on_modem_removed(self, path: object) -> None:
        if self._modem is None or str(path) != self._modem.path:
            return
        log.info("iPhone HFP modem was removed from oFono")
        self._powered_path = None
        self._unbind(emit=True)
        self._release_modem()
        self._set_state(CALLS_SEARCHING)
        self._retry_index = 0
        self._schedule_retry()

    # ---- internals: bring-up ------------------------------------------

    def _adopt(self, modem: ModemInfo) -> None:
        if self._modem is None or self._modem.path != modem.path:
            self._unbind(emit=True)
            self._release_modem()
            try:
                self._modem_match = self._ensure_transport().watch(
                    self._guard(self._on_modem_property),
                    interface=MODEM_IFACE, signal="PropertyChanged", path=modem.path,
                )
            except Exception:
                log.warning("could not watch the iPhone HFP modem", exc_info=True)
                self._schedule_retry()
                return
            log.info("found the iPhone's oFono HFP modem")
        self._modem = modem
        self._advance()

    def _power_down(self) -> None:
        """Let oFono drop the hands-free link BlueFerry asked it to open.

        Without this oFono keeps the HFP service-level connection after calls
        are disabled or the daemon quits, so call audio would keep coming to
        this computer with no BlueFerry call UI.
        """
        path, self._powered_path = self._powered_path, None
        if path is not None:
            self._send_power_down(path)

    def _send_power_down(self, path: str) -> None:
        if self._transport is None:
            return
        log.info("releasing the iPhone HFP modem (Powered=false)")
        try:
            self._transport.send(
                path, MODEM_IFACE, "SetProperty", "sv", ("Powered", boolean(False)),
            )
        except Exception:
            log.debug("could not power down the iPhone HFP modem", exc_info=True)

    def _release_modem(self) -> None:
        self._drop_phone_status()
        self._remove(self._modem_match)
        self._modem_match = None
        self._modem = None
        self._request_in_flight = False
        self._requested.clear()
        self._blocked = False
        self._power_failures = 0
        self._cancel_timer("_bringup_id")

    def _on_modem_property(self, name: object, value: object) -> None:
        if self._modem is None:
            return
        previous = self._modem
        self._modem = previous.updated(name, value)
        if not self._modem.powered:
            self._requested.clear()
        elif not self._modem.online:
            self._requested.discard("Online")
        progressed = (
            (self._modem.powered and not previous.powered)
            or (self._modem.online and not previous.online)
            or (self._modem.voice_ready and not previous.voice_ready)
        )
        if progressed:
            # oFono moved forward on its own; a pending backoff would only
            # delay the next bring-up step.
            self._cancel_timer("_retry_id")
            self._retry_index = 0
            self._blocked = False
            self._power_failures = 0
        if str(name) in {"Powered", "Online", "Interfaces"}:
            self._advance()

    def _advance(self) -> None:
        modem = self._modem
        if modem is None or not self._running:
            return
        if modem.powered:
            self._powered_path = modem.path
        # Independent of call control: needs only a powered modem.
        self._sync_phone_status()
        if modem.voice_ready:
            self._cancel_timer("_bringup_id")
            if self._bound_path != modem.path:
                self._bind(modem.path)
            if self._bound_path != modem.path:
                # Online alone is insufficient: without call-manager watches
                # the controller cannot accept operations or track calls.
                self._set_state(CALLS_CONNECTING)
                return
            self._set_state(CALLS_READY)
            return
        if self._bound_path is not None:
            # Online dropped or the phone disconnected: its calls are gone.
            log.info("iPhone HFP modem went offline")
            self._unbind(emit=True)
        if not self._blocked:
            self._set_state(CALLS_CONNECTING)
        if self._request_in_flight or self._retry_id is not None:
            return
        if not modem.powered:
            if "Powered" in self._requested:
                self._arm_bringup_watchdog()
                return
            if not self._reachable():
                # Do not page an absent phone; poke() retries when Classic
                # returns, and the steady poll covers a missed transition.
                self._schedule_retry(RETRY_STEADY_SEC)
                return
            self._request_modem_property("Powered")
        elif not modem.online:
            if "Online" in self._requested:
                self._arm_bringup_watchdog()
                return
            self._request_modem_property("Online")
        else:
            # Online, but VoiceCallManager has not been announced yet.
            self._arm_bringup_watchdog()

    def _request_modem_property(self, name: str) -> None:
        modem = self._modem
        if modem is None:
            return
        path = modem.path
        self._request_in_flight = True
        self._arm_bringup_watchdog()
        log.log(self._bringup_level(), "requesting iPhone HFP modem %s=true", name)

        def done(*_values: Any) -> None:
            self._request_in_flight = False
            self._requested.add(name)
            if name == "Powered":
                self._powered_path = path
            # oFono confirms through PropertyChanged; re-evaluate in case the
            # signal was delivered before this reply.
            self._advance()

        def failed(error: Exception) -> None:
            self._request_in_flight = False
            log.log(
                self._bringup_level(), "oFono rejected %s=true: %s", name, public_error(error),
            )
            if self._modem is not None and self._modem.path == path:
                self._cancel_timer("_bringup_id")
                if name == "Powered":
                    self._power_failed()
                else:
                    self._schedule_retry()

        def stale() -> None:
            # oFono refuses Powered=false while this request is pending, so a
            # stop in between can only release the modem once it has replied.
            if name == "Powered" and not self._running:
                self._send_power_down(path)

        self._call(
            path, MODEM_IFACE, "SetProperty", "sv", (name, boolean(True)), done, failed,
            stale,
        )

    def _bringup_level(self) -> int:
        # The oFono/WirePlumber profile race can keep bring-up failing for
        # hours. Report the first attempt of a series at INFO and its
        # repetitions at DEBUG; progress or success resets the series.
        return logging.INFO if self._retry_index == 0 else logging.DEBUG

    def _arm_bringup_watchdog(self) -> None:
        if self._bringup_id is None:
            self._bringup_id = self._schedule(BRINGUP_TIMEOUT_SEC, self._bringup_timeout)

    def _power_failed(self) -> None:
        """Retry a failed Powered, reporting a BlueZ HFP conflict if it persists.

        oFono's Powered=true waits for the HFP service-level connection. When
        bluetoothd runs its experimental hands-free plugin it can take the
        iPhone's HFP channel first, and every retry pages the phone again
        with little chance of success. A few failures in a row with that
        plugin loaded are reported, and the retries slow down.
        """
        self._power_failures += 1
        self._request_in_flight = False
        self._requested.clear()
        self._cancel_timer("_bringup_id")
        if self._power_failures >= POWER_ATTEMPTS_BEFORE_CONFLICT and self._bluez_conflict():
            if not self._blocked:
                log.warning(
                    "iPhone HFP modem did not power up in %d attempts and bluetoothd "
                    "runs its own HFP hands-free plugin (-E without -P hfp), which "
                    "competes with oFono for the call channel; start bluetoothd with "
                    "-P hfp. Retrying every %d minutes and when the iPhone reconnects.",
                    self._power_failures, CONFLICT_RETRY_SEC // 60,
                )
            self._blocked = True
            self._set_state(CALLS_BLUEZ_CONFLICT)
            self._schedule_retry(CONFLICT_RETRY_SEC)
            return
        if self._blocked:
            # bluetoothd no longer loads the plugin; back to the normal pace.
            self._blocked = False
            self._retry_index = 0
            self._set_state(CALLS_CONNECTING)
        self._schedule_retry()

    def _bluez_conflict(self) -> bool:
        try:
            return bool(self._hfp_conflict())
        except Exception:
            log.debug("BlueZ HFP plugin check failed", exc_info=True)
            return False

    def _bringup_timeout(self) -> bool:
        self._bringup_id = None
        if self._running and self._state != CALLS_READY:
            log.log(
                self._bringup_level(),
                "iPhone HFP modem did not come online in %ds; retrying", BRINGUP_TIMEOUT_SEC,
            )
            self._request_in_flight = False
            self._requested.clear()
            if self._modem is not None and not self._modem.powered:
                self._power_failed()
            else:
                self._schedule_retry()
        return False

    # ---- internals: calls -----------------------------------------------

    def _bind(self, path: str) -> None:
        self._unbind(emit=True)
        self._snapshot_changes = set()
        transport = self._ensure_transport()
        try:
            for signal, handler in (
                ("CallAdded", self._on_call_added),
                ("CallRemoved", self._on_call_removed),
            ):
                self._vcm_matches.append(transport.watch(
                    self._guard(handler, binding=True),
                    interface=VOICE_CALL_MANAGER_IFACE, signal=signal, path=path,
                ))
        except Exception:
            log.warning("could not watch iPhone calls", exc_info=True)
            for match in self._vcm_matches:
                self._remove(match)
            self._vcm_matches = []
            self._snapshot_changes = None
            self._schedule_retry()
            return
        self._bound_path = path
        self._retry_index = 0
        log.info("iPhone HFP modem is online; call control ready")
        self._call(
            path, VOICE_CALL_MANAGER_IFACE, "GetCalls", "", (),
            self._guard(self._on_existing_calls, binding=True),
            self._guard(self._on_existing_calls_failed, binding=True),
        )
        self._maximize_call_volume(path)

    def _unbind(self, *, emit: bool) -> None:
        # A modem can disappear and return at the same object path. Its old
        # snapshot and queued signals must not populate the new binding.
        self._calls_generation += 1
        self._snapshot_changes = None
        for match in self._vcm_matches:
            self._remove(match)
        self._vcm_matches = []
        for match in self._call_matches.values():
            self._remove(match)
        self._call_matches.clear()
        self._bound_path = None
        ended = list(self._calls.values())
        self._calls.clear()
        if emit and ended:
            for record in ended:
                self._emit("call_ended", record)
            self._calls_changed()

    def _maximize_call_volume(self, path: str) -> None:
        """Best effort: oFono's 50 % default is inaudibly faint on iPhone."""
        def ignored(*_values: Any) -> None:
            return None

        def failed(error: Exception) -> None:
            log.info("could not raise oFono call volume: %s", public_error(error))

        for name in ("SpeakerVolume", "MicrophoneVolume"):
            self._call(
                path, CALL_VOLUME_IFACE, "SetProperty", "sv",
                (name, byte(CALL_VOLUME_MAX)), ignored, failed,
            )

    def _on_existing_calls(self, calls: object = ()) -> None:
        changed, self._snapshot_changes = self._snapshot_changes or set(), None
        for entry in calls if isinstance(calls, list | tuple) else ():
            if isinstance(entry, list | tuple) and len(entry) == 2:
                # Signals delivered while GetCalls was pending are newer than
                # its snapshot, including removals of calls not yet observed.
                if call_id_from_path(entry[0]) in changed:
                    continue
                self._on_call_added(entry[0], entry[1])

    def _on_existing_calls_failed(self, error: Exception) -> None:
        self._snapshot_changes = None
        log.warning("oFono GetCalls failed: %s", public_error(error))

    def _resolved(self, record: CallRecord) -> CallRecord:
        name = ""
        if record.number:
            try:
                name = self._resolve_contact(record.number) or ""
            except Exception:
                log.debug("contact lookup for a call failed", exc_info=True)
        return replace(record, contact_name=remote_text(name))

    def _on_call_added(self, path: object, properties: object) -> None:
        record = parse_call(path, properties)
        if record is None:
            log.warning("ignoring malformed oFono call")
            return
        if self._snapshot_changes is not None:
            self._snapshot_changes.add(record.call_id)
        if record.call_id in self._calls and isinstance(properties, Mapping):
            # GetCalls and CallAdded can race after binding.
            for key, value in properties.items():
                self._on_call_property(record.call_id, key, value)
            return
        if len(self._calls) >= MAX_TRACKED_CALLS:
            log.warning("ignoring an oFono call beyond the tracking limit")
            return
        try:
            match = self._ensure_transport().watch(
                self._guard(
                    lambda name, value, call_id=record.call_id:
                        self._on_call_property(call_id, name, value),
                    binding=True,
                ),
                interface=VOICE_CALL_IFACE, signal="PropertyChanged", path=record.path,
            )
        except Exception:
            log.warning("could not watch an iPhone call", exc_info=True)
            return
        self._call_matches[record.call_id] = match
        record = self._resolved(record)
        self._calls[record.call_id] = record
        log.info("call %s added (%s, %s)", record.call_id, record.direction, record.state)
        self._emit("call_incoming" if record.ringing else "call_changed", record)
        self._calls_changed()

    def _on_call_property(self, call_id: str, name: object, value: object) -> None:
        if self._snapshot_changes is not None:
            self._snapshot_changes.add(call_id)
        current = self._calls.get(call_id)
        if current is None:
            return
        updated = current.with_property(name, value)
        if updated == current:
            return
        if updated.number != current.number:
            updated = self._resolved(updated)
        self._calls[call_id] = updated
        if updated.state != current.state:
            log.info("call %s: %s -> %s", call_id, current.state, updated.state)
            if updated.state == "active" and self._bound_path is not None:
                # iOS can reset the gain when SCO audio starts.
                self._maximize_call_volume(self._bound_path)
        self._emit("call_changed", updated)
        self._calls_changed()

    def _on_call_removed(self, path: object) -> None:
        call_id = call_id_from_path(path)
        if call_id is None:
            return
        if self._snapshot_changes is not None:
            self._snapshot_changes.add(call_id)
        record = self._calls.pop(call_id, None)
        self._remove(self._call_matches.pop(call_id, None))
        if record is None:
            return
        log.info("call %s ended", call_id)
        self._emit("call_ended", record)
        self._calls_changed()

    # ---- internals: phone battery, signal, operator -----------------------

    def _sync_phone_status(self) -> None:
        """Watch the status interfaces the powered modem currently lists.

        Only ``Powered=false``, a vanished interface, modem removal, an oFono
        owner change, or ``stop()`` clear the values; Online dropping does not.
        """
        modem = self._modem
        if modem is None or not self._running or not modem.powered:
            self._drop_phone_status()
            return
        if self._phone_path not in (None, modem.path):
            self._drop_phone_status()
        wanted = {iface for iface in PHONE_STATUS_IFACES if iface in modem.interfaces}
        previous = self._phone
        for iface in [iface for iface in self._phone_matches if iface not in wanted]:
            self._unwatch_phone(iface)
        self._phone_changed(previous)
        for iface in PHONE_STATUS_IFACES:
            if iface in wanted and iface not in self._phone_matches:
                self._watch_phone(modem.path, iface)

    def _watch_phone(self, path: str, iface: str) -> None:
        token = object()

        def changed(name: object, value: object) -> None:
            if self._phone_tokens.get(iface) is not token:
                return
            self._apply_phone(
                lambda status: status.with_handsfree(name, value)
                if iface == HANDSFREE_IFACE else status.with_network(name, value)
            )

        try:
            match = self._ensure_transport().watch(
                self._guard(changed), interface=iface, signal="PropertyChanged", path=path,
            )
        except Exception:
            if f"watch:{iface}" not in self._phone_errors:
                log.info("could not watch the iPhone's %s properties", iface, exc_info=True)
            self._phone_errors.add(f"watch:{iface}")
            return
        self._phone_path = path
        self._phone_matches[iface] = match
        self._phone_tokens[iface] = token
        if not self._phone_announced:
            self._phone_announced = True
            log.info("reading the iPhone's battery and signal indicators through oFono")

        self._read_phone(path, iface, token, retry=True)

    def _read_phone(self, path: str, iface: str, token: object, *, retry: bool) -> None:
        """GetProperties for one watched interface, with one delayed retry.

        The first Handsfree.GetProperties makes oFono send AT+CNUM to the
        phone; a concurrent call meanwhile fails with org.ofono.Error.InProgress,
        and a slow phone can fail it outright. PropertyChanged alone would
        leave the battery unknown until it next changes, so a failure other
        than a vanished interface schedules a single retry.
        """
        def current() -> bool:
            return self._phone_tokens.get(iface) is token

        def replied(properties: object = None) -> None:
            if not current():
                return
            self._phone_errors.discard(f"get:{iface}")
            self._apply_phone(lambda status: apply_properties(status, iface, properties))

        def failed(error: Exception) -> None:
            if not current():
                return
            key = f"get:{iface}"
            if key not in self._phone_errors:
                self._phone_errors.add(key)
                if interface_missing(error):
                    log.debug("oFono %s disappeared before GetProperties", iface)
                else:
                    log.info("oFono %s GetProperties failed: %s", iface, public_error(error))
            if retry and not interface_missing(error):
                self._schedule_phone_retry(path, iface, token)

        self._call(path, iface, "GetProperties", "", (), replied, failed)

    def _schedule_phone_retry(self, path: str, iface: str, token: object) -> None:
        if iface in self._phone_retries:
            return

        def fire() -> bool:
            self._phone_retries.pop(iface, None)
            if self._running and self._phone_tokens.get(iface) is token:
                self._read_phone(path, iface, token, retry=False)
            return False

        self._phone_retries[iface] = self._schedule(RETRY_STEADY_SEC, fire)

    def _cancel_phone_retry(self, iface: str) -> None:
        source = self._phone_retries.pop(iface, None)
        if source is None:
            return
        try:
            self._cancel(source)
        except Exception:
            log.debug("could not remove phone-status retry timer", exc_info=True)

    def _unwatch_phone(self, iface: str) -> None:
        self._cancel_phone_retry(iface)
        self._remove(self._phone_matches.pop(iface, None))
        self._phone_tokens.pop(iface, None)
        self._phone = (
            self._phone.without_handsfree() if iface == HANDSFREE_IFACE
            else self._phone.without_network()
        )
        if not self._phone_matches:
            self._phone_path = None

    def _drop_phone_status(self) -> None:
        previous = self._phone
        for iface in list(self._phone_matches):
            self._unwatch_phone(iface)
        self._phone = PhoneStatus()
        self._phone_path = None
        self._phone_announced = False
        # A new modem session logs its own first failure again.
        self._phone_errors.clear()
        if self._running:
            self._phone_changed(previous)

    def _apply_phone(self, update: Callable[[PhoneStatus], PhoneStatus]) -> None:
        previous = self._phone
        self._phone = update(previous)
        self._phone_changed(previous)

    def _phone_changed(self, previous: PhoneStatus) -> None:
        # Only published values matter: e.g. a strength update while the
        # phone is unregistered changes nothing a client can see.
        if previous.to_status() == self._phone.to_status():
            return
        try:
            self._on_phone_status(self._phone)
        except Exception:
            log.exception("phone status callback failed")

    # ---- call control -----------------------------------------------------

    def _require_ready(self) -> str:
        if not self.enabled:
            raise CallsDisabledError(CALLS_DISABLED_HINT)
        if self._state != CALLS_READY or self._bound_path is None:
            detail = {
                CALLS_UNAVAILABLE: "oFono is not running",
                CALLS_SEARCHING: "oFono has no hands-free modem for the iPhone",
                CALLS_CONNECTING: "the iPhone's hands-free modem is not online yet",
                CALLS_BLUEZ_CONFLICT: "bluetoothd's own HFP plugin holds the call channel",
            }.get(self._state, "call control is not ready")
            raise CallsUnavailableError(f"phone calls are unavailable: {detail}")
        return self._bound_path

    def _find(self, call_id: object) -> CallRecord:
        selected = validate_call_id(call_id)
        record = self._calls.get(selected)
        if record is None:
            raise NotFoundError("call no longer exists")
        return record

    def _control(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: tuple[Any, ...],
        success: Success,
        failure: Failure,
        *,
        result: Callable[..., Any] = lambda *_values: None,
    ) -> None:
        def replied(*values: Any) -> None:
            try:
                success(result(*values))
            except Exception as error:
                failure(error)

        def failed(error: Exception) -> None:
            if interface == VOICE_CALL_IFACE and interface_missing(error):
                # The call object vanished (ended) between listing and acting.
                log.info("oFono %s: the call no longer exists", method)
                failure(NotFoundError("call no longer exists"))
                return
            log.warning("oFono %s failed: %s", method, public_error(error))
            failure(OperationFailedError("Call", error))

        # Unlike state-tracking requests, a control reply always belongs to
        # its D-Bus caller, even across an oFono reset; dropping it would
        # leave the client waiting for its own timeout.
        try:
            self._ensure_transport().call(
                path, interface, method, signature, args, replied, failed,
            )
        except Exception as error:
            failed(error)

    def answer(self, call_id: object, success: Success, failure: Failure) -> None:
        modem = self._require_ready()
        record = self._find(call_id)
        if record.state == "incoming":
            self._control(record.path, VOICE_CALL_IFACE, "Answer", "", (), success, failure)
        elif record.state == "waiting":
            # A waiting call cannot be answered directly; hold the current one.
            self._control(
                modem, VOICE_CALL_MANAGER_IFACE, "HoldAndAnswer", "", (), success, failure,
            )
        else:
            raise InvalidArgumentsError("call is not ringing")

    def hangup(self, call_id: object, success: Success, failure: Failure) -> None:
        """Hang up, or decline a ringing call (oFono uses UDUB for waiting)."""
        self._require_ready()
        record = self._find(call_id)
        self._control(record.path, VOICE_CALL_IFACE, "Hangup", "", (), success, failure)

    def hangup_all(self, success: Success, failure: Failure) -> None:
        modem = self._require_ready()
        self._control(modem, VOICE_CALL_MANAGER_IFACE, "HangupAll", "", (), success, failure)

    def dial(self, number: object, success: Success, failure: Failure) -> None:
        normalized = normalize_dial_number(number)
        modem = self._require_ready()
        log.info("dialing through the iPhone (%d-character number)", len(normalized))

        def dialed(path: object = "") -> str:
            return call_id_from_path(path) or ""

        self._control(
            modem, VOICE_CALL_MANAGER_IFACE, "Dial", "ss", (normalized, ""),
            success, failure, result=dialed,
        )

    def send_tones(
        self, call_id: object, tones: object, success: Success, failure: Failure,
    ) -> None:
        selected = validate_dtmf(tones)
        modem = self._require_ready()
        record = self._find(call_id)
        if record.state != "active":
            raise InvalidArgumentsError("DTMF tones need an active call")
        self._control(
            modem, VOICE_CALL_MANAGER_IFACE, "SendTones", "s", (selected,), success, failure,
        )

    def swap(self, success: Success, failure: Failure) -> None:
        modem = self._require_ready()
        self._control(modem, VOICE_CALL_MANAGER_IFACE, "SwapCalls", "", (), success, failure)

    def hold_and_answer(self, success: Success, failure: Failure) -> None:
        modem = self._require_ready()
        if not any(record.state == "waiting" for record in self._calls.values()):
            raise InvalidArgumentsError("no call is waiting")
        self._control(
            modem, VOICE_CALL_MANAGER_IFACE, "HoldAndAnswer", "", (), success, failure,
        )
