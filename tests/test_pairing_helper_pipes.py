"""Exercise pairing IPC with real pipes, without starting Bluetooth helpers."""
from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest

from blueferry import setup_client

_MAC = "02:00:00:00:00:01"
_CONFIRMATION = {"event": "confirmation", "passkey": "123456"}
_FAILURE = {
    "ok": False,
    "error": "Bluetooth confirmation did not complete: canceled",
    "report_path": "/tmp/example-quirks.json",
}


@pytest.fixture
def helper(monkeypatch):
    popen = subprocess.Popen
    processes = []

    def launch(code, *, bufsize=None):
        def spawn(_command, **kwargs):
            if bufsize is not None:
                kwargs["bufsize"] = bufsize
            process = popen([sys.executable, "-c", code], **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(setup_client.subprocess, "Popen", spawn)
        return processes

    yield launch
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except BrokenPipeError:
                pass


def _emit(event):
    return f"print({json.dumps(event)!r}, flush=True)\n"


def _assert_closed(process):
    assert process.poll() is not None
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("bufsize", [1, 4096], ids=["write-fails", "flush-fails"])
def test_exit_during_confirmation_preserves_helper_error_and_closes_pipes(
    helper, accepted, bufsize,
):
    processes = helper(
        "import sys\n" + _emit(_CONFIRMATION) + _emit(_FAILURE) + "sys.exit(2)\n",
        bufsize=bufsize,
    )

    def confirm(_passkey):
        # Model the helper exiting while its confirmation is still on screen.
        processes[0].wait(timeout=5)
        return accepted

    with pytest.raises(setup_client.PairingError) as raised:
        setup_client.SetupClient().complete_isolated(_MAC, confirmation=confirm)

    assert str(raised.value) == _FAILURE["error"]
    assert raised.value.report_path == _FAILURE["report_path"]
    _assert_closed(processes[0])


def test_exit_during_confirmation_without_result_preserves_stderr(helper):
    processes = helper(
        "import sys\n" + _emit(_CONFIRMATION)
        + "print('helper failure detail', file=sys.stderr, flush=True)\nsys.exit(7)\n"
    )

    def confirm(_passkey):
        processes[0].wait(timeout=5)
        return True

    with pytest.raises(setup_client.PairingError) as raised:
        setup_client.SetupClient().complete_isolated(_MAC, confirmation=confirm)

    assert "status 7" in str(raised.value)
    assert "helper failure detail" in str(raised.value)
    _assert_closed(processes[0])


def test_success_delivers_confirmation_and_closes_pipes(helper):
    outcome = {"ok": True, "device": {"mac": _MAC, "name": "Test phone"}}
    processes = helper(
        "import sys\n" + _emit(_CONFIRMATION)
        + "assert sys.stdin.readline() == 'yes\\n'\n" + _emit(outcome)
    )
    result = setup_client.SetupClient().complete_isolated(
        _MAC, confirmation=lambda _passkey: True,
    )
    assert result.device.mac == _MAC
    _assert_closed(processes[0])


def test_confirmation_callback_failure_closes_pipes_and_stops_helper(helper):
    processes = helper(
        "import sys\n" + _emit(_CONFIRMATION) + "sys.stdin.readline()\n"
    )

    def confirm(_passkey):
        raise RuntimeError("confirmation dialog closed")

    with pytest.raises(RuntimeError, match="confirmation dialog closed"):
        setup_client.SetupClient().complete_isolated(_MAC, confirmation=confirm)
    _assert_closed(processes[0])


@pytest.mark.parametrize("noisy", [False, True], ids=["idle", "continuous-output"])
def test_closed_confirmation_pipe_has_bounded_recovery(helper, monkeypatch, noisy):
    monkeypatch.setattr(setup_client, "PAIRING_HELPER_STOP_TIMEOUT_SECONDS", 0.1)
    code = "import os, time\nos.close(0)\n" + _emit(_CONFIRMATION)
    if noisy:
        code += (
            "deadline = time.monotonic() + 2\n"
            "while time.monotonic() < deadline:\n"
            "    print('{}', flush=True)\n"
        )
    else:
        code += "time.sleep(2)\n"
    processes = helper(code)

    with pytest.raises(setup_client.PairingError, match="stopped accepting confirmation"):
        setup_client.SetupClient().complete_isolated(
            _MAC, confirmation=lambda _passkey: True,
        )

    _assert_closed(processes[0])
    assert processes[0].returncode < 0, "helper must be stopped before its two-second exit"


def test_closed_confirmation_pipe_does_not_prompt_again_or_accept_success(helper):
    outcome = {"ok": True, "device": {"mac": _MAC}}
    processes = helper(
        "import os\nos.close(0)\n" + _emit(_CONFIRMATION) * 2 + _emit(outcome)
    )
    confirmations = []

    def confirm(passkey):
        confirmations.append(passkey)
        return True

    with pytest.raises(setup_client.PairingError, match="Could not deliver pairing confirmation"):
        setup_client.SetupClient().complete_isolated(_MAC, confirmation=confirm)

    assert confirmations == [123456]
    _assert_closed(processes[0])


def test_error_with_full_output_queue_does_not_leave_reader_running(helper, tmp_path):
    marker = tmp_path / "output-written"
    original_threads = set(threading.enumerate())
    processes = helper(
        "import pathlib, sys\n" + _emit(_CONFIRMATION) + _emit(_FAILURE)
        + "sys.stdout.write('{}\\n' * 1000)\nsys.stdout.flush()\n"
        + f"pathlib.Path({str(marker)!r}).touch()\n"
        + "sys.stdin.readline()\n"
    )

    def confirm(_passkey):
        # Wait until the helper has filled the bounded reader queue. The pipe
        # still has room for the remaining small records on Linux.
        for _ in range(500):
            if marker.exists():
                return True
            threading.Event().wait(0.01)
        pytest.fail("helper did not finish writing its output")

    with pytest.raises(setup_client.PairingError, match="Bluetooth confirmation"):
        setup_client.SetupClient().complete_isolated(_MAC, confirmation=confirm)

    _assert_closed(processes[0])
    assert not [
        thread for thread in threading.enumerate()
        if thread not in original_threads and thread.name.startswith("blueferry-pairing-")
    ]
