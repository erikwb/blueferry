"""Keep iPhone audio local by managing one WirePlumber configuration fragment."""
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
from blueferry.service_manager import (
    ServiceManagerUnavailableError,
    user_service_manager,
)

log = logging.getLogger(__name__)

FRAGMENT_NAME = "99-blueferry-keep-phone-audio.conf"
LEGACY_FRAGMENT_NAME = "90-blueferry-keep-phone-audio.conf"
# 99- loads after other bluetooth auto-connect fragments.
#
# Without calls, this computer offers only source/gateway roles to the phone:
# no a2dp_sink (music stays on the phone) and no hfp_hf/hsp_hs (calls stay on
# the phone). With phone calls enabled the hands-free roles are kept so
# oFono's HFP modem can carry call audio here, while a2dp_sink is still
# stripped: music stays on the phone, calls can come to the PC. Auto-connect
# stays empty in both cases; BlueFerry's call controller asks oFono to bring
# up HFP (Modem.Powered) instead of WirePlumber paging the phone.
BASE_ROLES = ("a2dp_source", "hfp_ag", "bap_source")
CALL_ROLES = ("hfp_hf", "hsp_hs")


def fragment_text(*, allow_calls: bool = False) -> str:
    roles = BASE_ROLES + (CALL_ROLES if allow_calls else ())
    calls_note = (
        "# BlueFerry phone calls are enabled: keep the hands-free roles (hfp_hf,\n"
        "# hsp_hs) so calls can reach this computer; a2dp_sink stays off.\n"
        if allow_calls
        else ""
    )
    return f"""\
# Managed by BlueFerry. To remove this policy, set
# BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false in BlueFerry's local.env and
# restart blueferry.service.
{calls_note}monitor.bluez.properties = {{
  override.bluez5.roles = [ {" ".join(roles)} ]
}}
monitor.bluez.rules = [
  {{
    matches = [
      {{
        device.name = "~bluez_card.*"
        device.icon-name = "phone"
      }}
      {{
        device.name = "~bluez_card.*"
        device.icon-name = "audio-card-phone"
      }}
    ]
    actions = {{
      update-props = {{
        bluez5.auto-connect = [ ]
      }}
    }}
  }}
]
"""


FRAGMENT_TEXT = fragment_text(allow_calls=False)
CALLS_FRAGMENT_TEXT = fragment_text(allow_calls=True)
MAX_FRAGMENT_BYTES = 16 * 1024

ActiveCheck = Callable[[], bool]
SupportedCheck = Callable[[], bool]
Restart = Callable[[], None]
RestartHint = Callable[[], str | None]

VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.\d+)?(?!\d)")


def fragment_path() -> Path:
    """Return the WirePlumber 0.5 user-fragment path for this process."""
    configured = os.environ.get("WIREPLUMBER_CONFIG_DIR", "")
    roots = [value for value in configured.split(os.pathsep) if value]
    if roots:
        root = Path(roots[0]).expanduser()
    else:
        root = Path(
            os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        ) / "wireplumber"
    return root / "wireplumber.conf.d" / FRAGMENT_NAME


def legacy_fragment_path(path: Path) -> Path:
    return path.with_name(LEGACY_FRAGMENT_NAME)


def _wireplumber_active() -> bool:
    return user_service_manager(run_command).is_active("wireplumber", timeout=5)


def _manual_restart_hint() -> str | None:
    return user_service_manager(run_command).manual_restart_hint("WirePlumber")


def _parse_wireplumber_version(output: str) -> tuple[int, int] | None:
    for line in output.splitlines():
        normalized = line.strip().lower()
        if not (
            normalized.startswith("wireplumber ")
            or normalized.startswith("compiled with libwireplumber ")
        ):
            continue
        match = VERSION_PATTERN.search(normalized)
        if match is not None:
            return int(match.group(1)), int(match.group(2))
    return None


def _wireplumber_05_or_newer() -> bool:
    try:
        result = run_command(
            ["/usr/bin/wireplumber", "--version"],
            timeout=5,
        )
    except CommandError:
        return False
    version = _parse_wireplumber_version(f"{result.stdout}\n{result.stderr}")
    if version is None:
        log.warning("could not determine the active WirePlumber version")
        return False
    return version >= (0, 5)


def _restart_wireplumber(*, wait: bool = False) -> None:
    user_service_manager(run_command).try_restart("wireplumber", wait=wait)


def _matches(path: Path, expected: str) -> bool:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FRAGMENT_BYTES:
            return False
        return path.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


def _write_fragment(path: Path, value: str) -> None:
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_FRAGMENT_BYTES:
        raise ValueError("WirePlumber fragment exceeds the size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class WirePlumberPhoneAudioPolicy:
    """Reconcile BlueFerry's owned WirePlumber role override."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        active: ActiveCheck = _wireplumber_active,
        supported: SupportedCheck = _wireplumber_05_or_newer,
        restart: Restart | None = None,
        wait_for_restart: bool = False,
        manual_restart_hint: RestartHint = _manual_restart_hint,
        allow_calls: bool | None = None,
    ) -> None:
        self.path = path or fragment_path()
        # Pairing and the daemon both construct the policy; both follow the
        # configured opt-in unless a caller decides explicitly.
        self.allow_calls = config.CALLS_ENABLED if allow_calls is None else allow_calls
        self.text = fragment_text(allow_calls=self.allow_calls)
        self._active = active
        self._supported = supported
        self._manual_restart_hint = manual_restart_hint
        self._restart = restart or (
            lambda: _restart_wireplumber(wait=wait_for_restart)
        )

    def reconcile(self, *, enabled: bool) -> bool:
        """Apply or remove the policy and reload WirePlumber on change.

        Returns whether an owned fragment changed. Failure is advisory: audio
        routing must not prevent BlueFerry's messaging transports from starting.
        """
        changed = False
        try:
            is_active = self._active()
            is_supported = self._supported()
            legacy = legacy_fragment_path(self.path)
            if enabled and is_supported:
                if not _matches(self.path, self.text):
                    _write_fragment(self.path, self.text)
                    changed = True
                    log.info("installed WirePlumber phone-audio policy: %s", self.path)
                if legacy.exists() or legacy.is_symlink():
                    legacy.unlink()
                    changed = True
                    log.info("removed legacy WirePlumber phone-audio fragment: %s", legacy)
            elif not enabled:
                for candidate in (self.path, legacy):
                    if candidate.exists() or candidate.is_symlink():
                        candidate.unlink()
                        changed = True
                        log.info("removed WirePlumber phone-audio policy: %s", candidate)
            else:
                log.info(
                    "WirePlumber is older than 0.5 or its version is unknown; "
                    "leaving its configuration unchanged"
                )
        except OSError:
            log.warning("could not reconcile WirePlumber phone-audio policy", exc_info=True)
            return False

        if not changed:
            return changed
        if not is_active:
            # Desktops that start PipeWire through a launcher rather than a
            # service manager cannot be restarted safely from here.
            if hint := self._manual_restart_hint():
                log.warning(hint)
            return changed
        try:
            self._restart()
            log.info("restarted WirePlumber after changing its Bluetooth roles")
        except ServiceManagerUnavailableError as error:
            log.warning("WirePlumber policy changed; %s", error)
        except (CommandError, OSError):
            log.warning(
                "WirePlumber policy changed but its active service could not be restarted",
                exc_info=True,
            )
        return changed
