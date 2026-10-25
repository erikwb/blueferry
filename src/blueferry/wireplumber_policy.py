"""Clean up obsolete role restrictions and manage per-phone auto-connect rules."""
from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from blueferry import config
from blueferry.commands import run_command
from blueferry.errors import CommandError
from blueferry.service_manager import ServiceManagerUnavailableError, user_service_manager

log = logging.getLogger(__name__)

FRAGMENT_NAME = "99-blueferry-keep-phone-audio.conf"
LEGACY_FRAGMENT_NAME = "90-blueferry-keep-phone-audio.conf"
PREFERENCE_FRAGMENT_NAME = "zz-blueferry-phone-audio.conf"


def fragment_path() -> Path:
    """Return the location of BlueFerry's former WirePlumber fragment."""
    configured = os.environ.get("WIREPLUMBER_CONFIG_DIR", "")
    roots = [value for value in configured.split(os.pathsep) if value]
    if roots:
        root = Path(roots[0]).expanduser()
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "wireplumber"
    return root / "wireplumber.conf.d" / FRAGMENT_NAME


def _wireplumber_active() -> bool:
    return user_service_manager(run_command).is_active("wireplumber", timeout=5)


def _manual_restart_hint() -> str | None:
    return user_service_manager(run_command).manual_restart_hint("WirePlumber")


def _restart_wireplumber(*, wait: bool = False) -> None:
    user_service_manager(run_command).try_restart("wireplumber", wait=wait)


class LegacyPhoneAudioPolicy:
    """Remove only BlueFerry-owned fragments; never restrict Bluetooth roles."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        active: Callable[[], bool] = _wireplumber_active,
        restart: Callable[[], None] | None = None,
        wait_for_restart: bool = False,
        manual_restart_hint: Callable[[], str | None] = _manual_restart_hint,
    ) -> None:
        self.path = path or fragment_path()
        self._active = active
        self._restart = restart or (lambda: _restart_wireplumber(wait=wait_for_restart))
        self._manual_restart_hint = manual_restart_hint

    def remove(self) -> bool:
        """Clean up old installs and reload WirePlumber once when files changed.

        Removing the sink roles prevents normal Connect-first pairing. The
        retired BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE setting cannot re-enable
        the policy. Keep this migration for upgrades, including unpaired users.
        """
        changed = False
        for candidate in (self.path, self.path.with_name(LEGACY_FRAGMENT_NAME)):
            try:
                if candidate.exists() or candidate.is_symlink():
                    candidate.unlink()
                    changed = True
                    log.info("removed obsolete WirePlumber phone-audio policy: %s", candidate)
            except OSError:
                log.warning(
                    "could not remove obsolete phone-audio policy: %s", candidate, exc_info=True,
                )
        if not changed:
            return False
        try:
            if self._active():
                self._restart()
                log.info("restarted WirePlumber after removing Bluetooth role restrictions")
            elif hint := self._manual_restart_hint():
                log.warning(hint)
        except ServiceManagerUnavailableError as error:
            log.warning("WirePlumber policy removed; %s", error)
        except (CommandError, OSError):
            log.warning(
                "WirePlumber policy removed but its service could not be restarted", exc_info=True,
            )
        return changed


def phone_audio_fragment(mac: str) -> str:
    """Suppress only this phone's automatic profile dials; retain every role."""
    mac = mac.strip().upper()
    if not config.is_valid_mac(mac):
        raise ValueError("invalid Bluetooth device address")
    return (
        "# Managed by BlueFerry's remembered audio choice.\n"
        "# Use 'blueferry audio auto' to return to desktop-managed routing.\n"
        "monitor.bluez.rules = [\n"
        "  {\n"
        f'    matches = [ {{ api.bluez5.address = "{mac}" }} ]\n'
        "    actions = { update-props = { bluez5.auto-connect = [ ] } }\n"
        "  }\n"
        "]\n"
    )


def _supports_fragments() -> bool:
    try:
        result = run_command(["/usr/bin/wireplumber", "--version"], timeout=5)
    except CommandError:
        return False
    match = re.search(
        r"(?im)^(?:wireplumber|Compiled with libwireplumber)[ \t]+(\d+)\.(\d+)",
        result.stdout,
    )
    return bool(match and tuple(map(int, match.groups())) >= (0, 5))


class PhoneAudioAutoConnectPolicy:
    """Apply the per-phone rule on a worker while holding audio_activity.

    The same rule serves both remembered destinations: explicit PC requests
    still connect A2DP. Switching destinations therefore needs no reload.
    """

    def __init__(
        self, *, path: Path | None = None,
        supported: Callable[[], bool] = _supports_fragments,
        active: Callable[[], bool] = _wireplumber_active,
        restart: Callable[[], None] | None = None,
    ) -> None:
        self.path = path or fragment_path().with_name(PREFERENCE_FRAGMENT_NAME)
        self._supported = supported
        self._active = active
        self._restart = restart or (lambda: _restart_wireplumber(wait=True))
        self._reload_pending = False

    def configure(self, mac: str | None) -> str:
        """Return the rule's load state; errors are reported to the caller."""
        value = phone_audio_fragment(mac) if mac is not None else None
        changed = False
        service_active: bool | None = None
        if value is None:
            if self.path.exists() or self.path.is_symlink():
                self.path.unlink()
                changed = True
        else:
            if not self._supported():
                return "unavailable"
            matches = (
                not self.path.is_symlink() and self.path.is_file()
                and self.path.stat().st_size <= 4096
                and self.path.read_text(encoding="utf-8") == value
            )
            if not matches:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=".blueferry-audio-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                finally:
                    Path(name).unlink(missing_ok=True)
                changed = True
        self._reload_pending = self._reload_pending or changed
        if self._reload_pending:
            try:
                service_active = self._active()
                if service_active:
                    self._restart()
                else:
                    hint = _manual_restart_hint() or "Restart WirePlumber to load the audio rule"
                    log.warning(hint)
                    self._reload_pending = False
                    return "pending-reload" if value is not None else "desktop"
            except Exception:
                # A failed first opt-in must not leave an unsaved rule that
                # silently takes effect at the next desktop login.
                if value is not None and changed:
                    self.path.unlink(missing_ok=True)
                raise
            self._reload_pending = False
        if value is None:
            return "desktop"
        if service_active is None:
            service_active = self._active()
        return "active" if service_active else "pending-reload"
