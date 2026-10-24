"""Remove obsolete BlueFerry WirePlumber fragments that interfere with pairing."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from blueferry.commands import run_command
from blueferry.errors import CommandError
from blueferry.service_manager import ServiceManagerUnavailableError, user_service_manager

log = logging.getLogger(__name__)

FRAGMENT_NAME = "99-blueferry-keep-phone-audio.conf"
LEGACY_FRAGMENT_NAME = "90-blueferry-keep-phone-audio.conf"


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
