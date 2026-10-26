"""Run a GTK test scenario in a child process on a private Broadway display.

GTK runs in its own process: the suite also loads Qt and must not share
toolkit event loops or adopt the operator's Wayland/X11 display. The test
file is executed as a script with the scenario name as its argument.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_scenario(tmp_path: Path, script: str, scenario: str, timeout: float = 30) -> None:
    executable = shutil.which("gtk4-broadwayd")
    if executable is None:
        pytest.skip("GTK4 Broadway is not installed")
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    environment = dict(
        os.environ, XDG_RUNTIME_DIR=str(runtime), GDK_BACKEND="broadway",
        BROADWAY_DISPLAY=":0", GTK_A11Y="none", GSETTINGS_BACKEND="memory",
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        # Private XDG directories: a scenario never sees the developer's
        # plugins, logs, caches or settings.
        XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_STATE_HOME=str(tmp_path / "state"), XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    # Both the GTK display and its HTTP endpoint use private Unix sockets.
    daemon = subprocess.Popen(
        [executable, "--unixsocket", str(runtime / "http"), ":0"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 5
        while not (runtime / "broadway1.socket").exists():
            assert daemon.poll() is None, daemon.communicate()[0].decode()
            assert time.monotonic() < deadline, "Broadway did not create its display socket"
            time.sleep(0.02)
        result = subprocess.run(
            [sys.executable, script, scenario], env=environment,
            capture_output=True, text=True, timeout=timeout,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        daemon.terminate()
        try:
            daemon.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.communicate()
