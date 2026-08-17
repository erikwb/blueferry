"""Caller validation and abuse limits for the private session-bus API.

The session bus is still a same-Unix-user trust boundary. These checks prevent
cross-user access on unusually configured buses and bound accidental or
malicious request floods; they do not claim to authenticate native apps that
run under the daemon owner's UID.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import dbus

from blueferry.errors import AuthorizationError, RateLimitError

log = logging.getLogger(__name__)

_DBUS_NAME = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_DBUS_IFACE = "org.freedesktop.DBus"


@dataclass(frozen=True, slots=True)
class RateRule:
    attempts: int
    seconds: float


# Limits are intentionally generous for normal UI refreshes. Consequential or
# expensive operations have both short and long windows so reconnecting to the
# bus cannot turn a burst into an unlimited stream.
_RULES: dict[str, tuple[RateRule, ...]] = {
    "status": (RateRule(600, 60),),
    "read": (RateRule(240, 60),),
    "send": (RateRule(30, 60), RateRule(200, 3_600)),
    "contact-sync": (RateRule(6, 600), RateRule(20, 3_600)),
    "settings": (RateRule(30, 60),),
    "conversation-delete": (RateRule(60, 60), RateRule(500, 3_600)),
    "destructive": (RateRule(6, 600),),
    "unlock": (RateRule(6, 600),),
}


class CallerGuard:
    """Authorize bus callers and apply per-caller plus service-wide quotas."""

    def __init__(
        self,
        bus=None,
        *,
        expected_uid: int | None = None,
        credential_provider: Callable[[str], Mapping] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._expected_uid = os.getuid() if expected_uid is None else expected_uid
        self._clock = clock
        self._credential_provider = credential_provider or self._bus_credentials
        self._credentials: dict[str, Mapping] = {}
        self._attempts: dict[tuple[str, str, float], deque[float]] = defaultdict(deque)
        self._owner_match = None
        if bus is not None:
            self._owner_match = bus.add_signal_receiver(
                self._name_owner_changed,
                dbus_interface=_DBUS_IFACE,
                signal_name="NameOwnerChanged",
                bus_name=_DBUS_NAME,
            )

    def _bus_credentials(self, sender: str) -> Mapping:
        if self._bus is None:
            raise AuthorizationError("caller credentials are unavailable")
        daemon = dbus.Interface(
            self._bus.get_object(_DBUS_NAME, _DBUS_PATH),
            _DBUS_IFACE,
        )
        return daemon.GetConnectionCredentials(sender)

    def _name_owner_changed(self, name, old_owner, new_owner) -> None:
        if str(name).startswith(":") and old_owner and not new_owner:
            self.forget(str(name))

    def forget(self, sender: str) -> None:
        self._credentials.pop(sender, None)
        for key in [key for key in self._attempts if key[0] == sender]:
            self._attempts.pop(key, None)

    def close(self) -> None:
        if self._owner_match is not None:
            try:
                self._owner_match.remove()
            except Exception:
                log.debug("could not remove D-Bus caller watch", exc_info=True)
            self._owner_match = None
        self._credentials.clear()
        self._attempts.clear()

    def authorize(self, sender: str | None, action: str) -> None:
        identity = str(sender or "")
        if not identity.startswith(":"):
            raise AuthorizationError("D-Bus caller identity is unavailable")

        credentials = self._credentials.get(identity)
        if credentials is None:
            try:
                credentials = self._credential_provider(identity)
            except Exception as error:
                log.warning("could not obtain D-Bus caller credentials: %s", error)
                raise AuthorizationError(
                    "D-Bus caller credentials could not be verified"
                ) from None
            self._credentials[identity] = credentials

        try:
            uid = int(credentials["UnixUserID"])
        except (KeyError, TypeError, ValueError):
            raise AuthorizationError("D-Bus caller has no verified Unix UID") from None
        if uid != self._expected_uid:
            log.warning("rejected D-Bus caller owned by UID %d", uid)
            raise AuthorizationError("D-Bus caller is not authorized")

        self._check_rates(identity, action)

    def _check_rates(self, sender: str, action: str) -> None:
        rules = _RULES.get(action)
        if rules is None:
            raise RuntimeError(f"unknown D-Bus authorization action: {action}")
        now = self._clock()
        for scope in (sender, "*"):
            for rule in rules:
                key = (scope, action, rule.seconds)
                attempts = self._attempts[key]
                cutoff = now - rule.seconds
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if len(attempts) >= rule.attempts:
                    raise RateLimitError(
                        "too many requests; wait before trying again"
                    )
        for scope in (sender, "*"):
            for rule in rules:
                self._attempts[(scope, action, rule.seconds)].append(now)
