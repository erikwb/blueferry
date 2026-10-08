"""Detection of bluetoothd's own HFP hands-free plugin (no real /proc)."""
from __future__ import annotations

import pytest

from blueferry.bluetooth_capabilities import (
    bluetoothd_argv,
    bluez_hfp_plugin_active,
    bluez_hfp_plugin_possible,
)

BTD = "/usr/libexec/bluetooth/bluetoothd"


@pytest.mark.parametrize("argv,expected", [
    (None, False),
    ([], False),
    ([BTD], False),
    ([BTD, "-E"], True),
    ([BTD, "--experimental"], True),
    ([BTD, "-E", "-P", "hfp"], False),
    ([BTD, "-E", "-Phfp"], False),
    ([BTD, "-E", "--noplugin=sap,hfp"], False),
    ([BTD, "-E", "--noplugin", "hf*"], False),
    ([BTD, "-E", "-P", "sap"], True),
    ([BTD, "-E", "-p", "a2dp,avrcp"], False),
    ([BTD, "-E", "--plugin=hfp"], True),
    ([BTD, "-E", "-p", "*", "-P", "hfp"], False),
    # The program name itself is never an option.
    (["-E"], False),
])
def test_hfp_plugin_activity_follows_bluetoothd_options(argv, expected) -> None:
    assert bluez_hfp_plugin_active(argv) is expected


def _process(root, pid, comm, argv) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(comm + "\n")
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")


def test_bluetoothd_is_found_by_process_name(tmp_path) -> None:
    (tmp_path / "self").mkdir()
    _process(tmp_path, 10, "bash", ["bash", "-E"])
    _process(tmp_path, 42, "bluetoothd", [BTD, "-E", "-P", "hfp"])

    assert bluetoothd_argv(tmp_path) == [BTD, "-E", "-P", "hfp"]


def test_missing_bluetoothd_or_proc_is_unknown(tmp_path) -> None:
    _process(tmp_path, 10, "bash", ["bash"])

    assert bluetoothd_argv(tmp_path) is None
    assert bluetoothd_argv(tmp_path / "missing") is None


@pytest.mark.parametrize("version,expected", [
    ("5.87", True), ("5.87.1", True), ("5.90", True), ("6.0", True),
    ("5.86", False), ("5.66", False),
    # Unknown is not evidence that the plugin is absent.
    ("", True), (None, True), ("unknown", True),
])
def test_hfp_plugin_needs_a_bluez_that_ships_it(version, expected) -> None:
    assert bluez_hfp_plugin_possible(version) is expected
