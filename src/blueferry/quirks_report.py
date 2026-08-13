"""Scrubbed pairing/adapter reports kept next to local history."""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from blueferry import config
from blueferry.bluetooth_capabilities import chipset_name, is_generic_product
from blueferry.private_files import atomic_write_private_text, ensure_private_directory

log = logging.getLogger(__name__)

REPORT_PREFIX = "quirks-"
REPORT_SUFFIX = ".json"
MAX_REPORTS = 10
MAX_REPORT_BYTES = 64 * 1024
MAX_ISSUE_URL_CHARS = 8000
GITHUB_ISSUE_NEW = "https://github.com/erikwb/blueferry/issues/new"
GITHUB_ISSUE_LABEL = "pairing-issue"

_MAC_COLON = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
_MAC_PATH = re.compile(r"(?i)dev_(?:[0-9a-f]{2}_){5}[0-9a-f]{2}")


def _home_path() -> Path | None:
    try:
        home = Path.home()
    except RuntimeError:
        return None
    if len(str(home)) < 2 or str(home) == "/":
        return None
    return home


def _redaction_prefixes() -> list[tuple[str, str]]:
    """Longest-first home and XDG prefixes to strip from diagnostic text."""
    home = _home_path()
    pairs: list[tuple[str, str]] = []

    def add(raw: str | Path | None, token: str) -> None:
        if raw is None:
            return
        text = str(Path(str(raw).strip()).expanduser()).rstrip("/")
        if len(text) < 2 or text == "/":
            return
        pairs.append((text, token))

    add(os.environ.get("XDG_CONFIG_HOME") or (home / ".config" if home else None),
        "$XDG_CONFIG_HOME")
    add(os.environ.get("XDG_STATE_HOME") or (home / ".local/state" if home else None),
        "$XDG_STATE_HOME")
    add(os.environ.get("XDG_CACHE_HOME") or (home / ".cache" if home else None),
        "$XDG_CACHE_HOME")
    add(os.environ.get("XDG_DATA_HOME") or (home / ".local/share" if home else None),
        "$XDG_DATA_HOME")
    add(home, "$HOME")
    add(os.environ.get("HOME"), "$HOME")
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for prefix, token in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if prefix in seen:
            continue
        seen.add(prefix)
        unique.append((prefix, token))
    return unique


def _replace_path_prefix(text: str, prefix: str, token: str) -> str:
    pattern = re.compile(
        r"(?<![\w.-])" + re.escape(prefix) + r"(?=/|$|\"|'|\s|:)",
    )
    return pattern.sub(token, text)


def scrub_text(value: str) -> str:
    """Remove Bluetooth addresses and home/XDG path prefixes from text."""
    text = str(value)
    for prefix, token in _redaction_prefixes():
        text = _replace_path_prefix(text, prefix, token)
    text = _MAC_COLON.sub("xx:xx:xx:xx:xx:xx", text)
    return _MAC_PATH.sub("dev_REDACTED", text)


def scrub_value(value: Any) -> Any:
    """Recursively redact Bluetooth addresses from a JSON-compatible value."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scrub_value(item) for key, item in value.items()}
    return value


def public_device(device: Any) -> dict[str, Any]:
    """Project a paired device without names, addresses, or object paths."""
    return {
        "likely_iphone": bool(getattr(device, "likely_iphone", False)),
        "paired": bool(getattr(device, "paired", False)),
        "trusted": bool(getattr(device, "trusted", False)),
        "connected": bool(getattr(device, "connected", False)),
        "services_resolved": bool(getattr(device, "services_resolved", False)),
        "ancs_uuid": bool(getattr(device, "ancs_bonded", False)),
    }


def session_environment() -> dict[str, str]:
    """Desktop session fields that are not personal identifiers."""
    return {
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
    }


def start_attempt(*, interactive: bool) -> dict[str, Any]:
    from blueferry import __version__

    attempt: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "blueferry": __version__,
        "pairing_path": "interactive" if interactive else "headless",
        "session": session_environment(),
        "_t0": time.monotonic(),
        "timeline": [],
    }
    mark(attempt, "start")
    return attempt


def mark(attempt: dict[str, Any] | None, event: str, **fields: Any) -> None:
    """Append one timeline entry. ``t`` is seconds since pairing started."""
    if attempt is None:
        return
    origin = attempt.get("_t0")
    if not isinstance(origin, (int, float)):
        origin = time.monotonic()
        attempt["_t0"] = origin
    entry = {"t": round(time.monotonic() - origin, 3), "event": event}
    entry.update(fields)
    attempt.setdefault("timeline", []).append(entry)


def save_report(report: dict[str, Any], *, directory: Path | None = None) -> Path | None:
    """Write one scrubbed report beside the history database and keep ten."""
    try:
        target_dir = directory if directory is not None else config.STATE_DIR
        if directory is None:
            config.ensure_dirs()
        else:
            ensure_private_directory(target_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sequence = 1
        path = target_dir / f"{REPORT_PREFIX}{stamp}-{sequence:04d}{REPORT_SUFFIX}"
        while path.exists():
            sequence += 1
            path = target_dir / f"{REPORT_PREFIX}{stamp}-{sequence:04d}{REPORT_SUFFIX}"
        prepared = dict(report)
        origin = prepared.pop("_t0", None)
        timeline = prepared.get("timeline")
        if isinstance(origin, (int, float)):
            prepared["duration_s"] = round(time.monotonic() - origin, 3)
        elif isinstance(timeline, list) and timeline:
            last = timeline[-1]
            if isinstance(last, dict) and isinstance(last.get("t"), (int, float)):
                prepared["duration_s"] = last["t"]
        payload = json.dumps(
            scrub_value(prepared),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        atomic_write_private_text(path, payload, maximum_bytes=MAX_REPORT_BYTES)
        _prune_reports(target_dir)
        log.info("saved pairing report: %s", path)
        return path
    except Exception:
        log.warning("could not save pairing report", exc_info=True)
        return None


def list_reports(directory: Path | None = None) -> list[Path]:
    """Return pairing reports oldest-first."""
    target = directory if directory is not None else config.STATE_DIR
    try:
        if not target.is_dir():
            return []
        candidates = list(target.glob(f"{REPORT_PREFIX}*{REPORT_SUFFIX}"))
    except OSError:
        return []
    found: list[tuple[int, str, Path]] = []
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        found.append((info.st_mtime_ns, path.name, path))
    found.sort()
    return [path for _mtime, _name, path in found]


def latest_report(directory: Path | None = None) -> Path | None:
    reports = list_reports(directory)
    return reports[-1] if reports else None


def issue_report(directory: Path | None = None) -> Path | None:
    """Return the latest pairing report, including a successful setup."""
    return latest_report(directory)


def _prune_reports(directory: Path) -> None:
    extra = list_reports(directory)[:-MAX_REPORTS]
    for path in extra:
        try:
            path.unlink()
        except OSError:
            log.debug("could not remove old pairing report %s", path, exc_info=True)


def issue_instructions(report_path: Path | str) -> str:
    return (
        f"Attach this pairing report to the GitHub issue and include the "
        f"iPhone model and iOS version:\n{report_path}"
    )


def cli_issue_hint() -> str:
    return (
        "Run `blueferry pairing-issue` if you'd like to file an issue about pairing."
    )


def _read_report(path: Path | str | None) -> dict[str, Any]:
    target = Path(path) if path is not None else latest_report()
    if target is None:
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _adapter_title(controller: Any) -> str:
    if not isinstance(controller, dict):
        return "unknown adapter"
    vendor = str(controller.get("vendor") or "").strip()
    product = str(controller.get("product") or "").strip()
    bus_id = str(controller.get("usb_id") or controller.get("pci_id") or "").strip()
    chip = chipset_name(
        usb_id=str(controller.get("usb_id") or ""),
        pci_id=str(controller.get("pci_id") or ""),
        driver=str(controller.get("driver") or ""),
        product=product,
    )
    label = chip or (product if product and not is_generic_product(product) else "")
    if vendor and label:
        if vendor.casefold() in label.casefold():
            return label
        return f"{vendor} {label}"
    if label:
        return label
    if vendor and bus_id:
        return f"{vendor} {bus_id}"
    return vendor or bus_id or "unknown adapter"


def _profile_word(value: Any) -> str | None:
    if value is True:
        return "success"
    if value is False:
        return "fail"
    return None


def _outcome_title(outcome: Any) -> str:
    if not isinstance(outcome, dict):
        return "status unknown"
    map_state = _profile_word(outcome.get("map"))
    pbap_state = _profile_word(outcome.get("pbap"))
    ancs_state = _profile_word(outcome.get("ancs"))
    parts: list[str] = []
    if map_state and map_state == pbap_state:
        parts.append(f"MAP/PBAP {map_state}")
    else:
        if map_state:
            parts.append(f"MAP {map_state}")
        if pbap_state:
            parts.append(f"PBAP {pbap_state}")
    if ancs_state:
        parts.append(f"ANCS {ancs_state}")
    if parts:
        return ", ".join(parts)
    if outcome.get("bonded") is True and outcome.get("setup_complete") is False:
        return "bonded, setup failed"
    if outcome.get("setup_complete") is False or outcome.get("bonded") is False:
        return "pairing failed"
    return "status unknown"


def issue_title(report: dict[str, Any] | Path | str | None = None) -> str:
    payload = report if isinstance(report, dict) else _read_report(report)
    title = (
        f"Pairing issue: {_adapter_title(payload.get('controller'))} — "
        f"{_outcome_title(payload.get('outcome'))}"
    )
    return title[:256]


def issue_body(report: dict[str, Any] | Path | str | None = None) -> str:
    payload = report if isinstance(report, dict) else _read_report(report)
    return _issue_body_with_json(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )


def _issue_body_with_json(report_json: str) -> str:
    return (
        "iPhone model:\n"
        "iOS version:\n\n"
        "```json\n"
        f"{report_json}\n"
        "```\n"
    )


def issue_url(report: dict[str, Any] | Path | str | None = None) -> str:
    """GitHub new-issue URL with label, chipset title, and report JSON body."""
    payload = report if isinstance(report, dict) else _read_report(report)
    title = issue_title(payload)
    candidates = [
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    ]
    slim = dict(payload)
    slim.pop("timeline", None)
    candidates.append(
        json.dumps(slim, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
    for report_json in candidates:
        url = (
            f"{GITHUB_ISSUE_NEW}?"
            + urlencode(
                {
                    "labels": GITHUB_ISSUE_LABEL,
                    "title": title,
                    "body": _issue_body_with_json(report_json),
                }
            )
        )
        if len(url) <= MAX_ISSUE_URL_CHARS:
            return url
    return (
        f"{GITHUB_ISSUE_NEW}?"
        + urlencode(
            {
                "labels": GITHUB_ISSUE_LABEL,
                "title": title,
                "body": (
                    "iPhone model:\n"
                    "iOS version:\n\n"
                    "The pairing report was too large to embed. Attach the "
                    "local quirks-*.json file from BlueFerry's state directory.\n"
                ),
            }
        )
    )


def open_issue_page(url: str | None = None) -> bool:
    """Open the new-issue page in the default browser when possible."""
    import webbrowser

    try:
        return bool(webbrowser.open(url or issue_url()))
    except Exception:
        log.debug("could not open the GitHub issue page", exc_info=True)
        return False
