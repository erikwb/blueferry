"""In-app diagnostics use current configuration without accessing Bluetooth."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("explicit", [False, True])
def test_doctor_reads_saved_settings_and_honors_explicit_overrides(tmp_path, monkeypatch, explicit):
    from blueferry import config, doctor_report

    root = tmp_path / "blueferry"
    root.mkdir(mode=0o700)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"features": {"BLUEFERRY_SHOW_NOTIFICATION_CONTENT": False}}))
    settings.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # The GUI loaded this old file-backed value before the user saved "off".
    monkeypatch.setenv("BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "true")
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset(
        {"BLUEFERRY_SHOW_NOTIFICATION_CONTENT"} if explicit else {}
    ))
    command = [sys.executable, "-c", "from blueferry import config; print(config.SHOW_NOTIFICATION_CONTENT)"]
    fresh_environment = dict(os.environ)
    fresh_environment.pop("BLUEFERRY_SHOW_NOTIFICATION_CONTENT")
    run = subprocess.run
    fresh = run(command, env=fresh_environment, capture_output=True, text=True, check=True).stdout.strip()
    assert fresh == "False"
    # Exercise the actual doctor launch environment with a configuration-only
    # child, avoiding any real Bluetooth or keyring access.
    assert doctor_report.run_doctor(command=command).text == ("True" if explicit else fresh)


def test_stop_catches_a_child_still_starting_and_blocks_queued_runs(monkeypatch):
    import threading

    from blueferry import doctor_report

    runner = doctor_report.DoctorRunner()
    spawned, release = threading.Event(), threading.Event()
    popen = subprocess.Popen
    children, reports = [], []

    def delayed_popen(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        spawned.set()
        assert release.wait(5)
        return child

    monkeypatch.setattr(doctor_report.subprocess, "Popen", delayed_popen)
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    worker = threading.Thread(target=lambda: reports.append(runner.run(command=command)))
    worker.start()
    try:
        assert spawned.wait(5)
        runner.stop()  # Popen has not returned yet: no child registered.
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert children[0].poll() is not None
        assert len(reports) == 1 and not reports[0].ok
        assert not runner.run(command=command).ok
        assert len(children) == 1  # Queued work after stop never spawns.
    finally:
        release.set()
        for child in children:
            child.kill()
        worker.join(5)


def test_stopping_one_owner_does_not_cancel_another_and_ignoring_term_cannot_hang(tmp_path):
    import threading
    import time

    from blueferry.doctor_report import DoctorRunner

    owner, other = DoctorRunner(), DoctorRunner()
    ready = tmp_path / "ready"
    command = [sys.executable, "-c",
               "import signal, time, pathlib, sys; "
               "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)", str(ready)]
    worker = threading.Thread(target=lambda: owner.run(command=command))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        owner.stop()
        worker.join(5)
        assert not worker.is_alive()
        assert other.run(command=[sys.executable, "-c", "print('OK')"]).text == "OK"
    finally:
        owner.stop()
        worker.join(5)


def test_doctor_replaces_invalid_output_bytes():
    from blueferry.doctor_report import run_doctor

    report = run_doctor(command=[sys.executable, "-c", "import os; os.write(1, b'OK \\xff')"])
    assert report.ok and report.text.startswith("OK ")
