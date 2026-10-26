"""Companion tool launchers with fake which/Gio/subprocess; nothing is started."""
from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only to raise TimeoutExpired from a fake
from pathlib import Path

import pytest

from blueferry import companion_tools as tools
from blueferry.companion_tools import CommandResult, System


class FakeLauncher:
    def __init__(self, name: str, pid: int | None = 4242, error: Exception | None = None):
        self.name = name
        self.pid = pid
        self.error = error
        self.launches = 0

    def launch(self) -> int | None:
        self.launches += 1
        if self.error is not None:
            raise self.error
        return self.pid


class Fake:
    """Records every interaction; answers commands from a script."""

    def __init__(self, tmp_path: Path, *, installed=(), desktop=(), answers=None) -> None:
        self.runtime = tmp_path / "runtime"
        self.installed = set(installed)
        self.desktop = {name: FakeLauncher(name) for name in desktop}
        self.commands: dict[tuple[str, ...], FakeLauncher] = {}
        self.answers: dict[tuple[str, ...], CommandResult] = dict(answers or {})
        self.ran: list[list[str]] = []
        self.opened: list[str] = []
        self.killed: list[tuple[int, int]] = []
        self.hostname = "desk.lan"
        self.processes: dict[int, list[str]] = {}
        self.starts: dict[int, int] = {}
        self.mounts: set[Path] = set()
        self.on_run = None
        self.replaced: frozenset[str] = frozenset()

    def system(self) -> System:
        def runtime_dir() -> Path:
            self.runtime.mkdir(mode=0o700, exist_ok=True)
            return self.runtime

        def command_app(argv):
            launcher = FakeLauncher(" ".join(argv))
            self.commands[tuple(argv)] = launcher
            return launcher

        def run(argv, timeout):
            assert 0 < timeout <= 60
            self.ran.append(list(argv))
            if self.on_run is not None:
                self.on_run(list(argv))
            return self.answers.get(tuple(argv), CommandResult(0, ""))

        def terminate(pid, still_ours):
            if not still_ours(pid):
                raise ProcessLookupError(pid)
            self.killed.append((pid, signal.SIGTERM))
            self.processes.pop(pid, None)

        return System(
            which=lambda name: f"/usr/bin/{name}" if name in self.installed else None,
            desktop_app=self.desktop.get,
            command_app=command_app,
            run=run,
            open_uri=self.opened.append,
            hostname=lambda: self.hostname,
            runtime_dir=runtime_dir,
            process_argv=self.processes.get,
            process_start=self.starts.get,
            terminate=terminate,
            is_mount=lambda path: path in self.mounts,
            replaced_tools=lambda: self.replaced,
        )


# ---- mirroring ----------------------------------------------------------------

def test_mirroring_prefers_the_desktop_entry_and_tracks_its_pid(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"}, desktop={tools.UXPLAY_DESKTOP_ID})
    system = fake.system()
    state = tools.mirror_state(system)
    assert (state.installed, state.enabled, state.active) == (True, True, False)
    assert "Screen Mirroring" in state.subtitle

    result = tools.toggle_mirroring(system)
    assert result.ok and "Control Center" in result.message
    assert fake.desktop[tools.UXPLAY_DESKTOP_ID].launches == 1
    assert fake.commands == {}
    pid_file = fake.runtime / tools.MIRROR_PID_FILE
    assert pid_file.read_text() == "4242"
    assert oct(pid_file.stat().st_mode & 0o777) == "0o600"

    # gio-launch-desktop execs uxplay; both command lines count as running.
    fake.processes[4242] = ["/usr/libexec/gio-launch-desktop", "uxplay", "-n", "Desk"]
    running = tools.mirror_state(system)
    assert running.active and running.title == "Stop mirroring"

    stopped = tools.toggle_mirroring(system)
    assert stopped.ok and fake.killed == [(4242, signal.SIGTERM)]
    assert not pid_file.exists()
    assert tools.mirror_state(system).active is False


def test_mirroring_falls_back_to_uxplay_with_the_short_host_name(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    assert tools.toggle_mirroring(fake.system()).ok
    assert list(fake.commands) == [("uxplay", "-n", "desk", "-nh")]


def test_a_reused_or_dead_pid_is_never_signalled(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    system = fake.system()
    pid_file = system.runtime_dir() / tools.MIRROR_PID_FILE
    pid_file.write_text("4242")
    pid_file.chmod(0o600)
    fake.processes[4242] = ["/usr/bin/firefox"]
    assert tools.mirroring_pid(system) is None
    assert not pid_file.exists()
    # Not running, so the click starts a new UxPlay instead of killing.
    assert tools.toggle_mirroring(system).ok
    assert fake.killed == [] and len(fake.commands) == 1


def test_only_a_uxplay_program_with_the_recorded_start_time_is_stopped(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    system = fake.system()
    pid_file = system.runtime_dir() / tools.MIRROR_PID_FILE
    fake.starts[4242] = 1000
    assert tools.toggle_mirroring(system).ok
    assert pid_file.read_text() == "4242 1000"

    # The pid now belongs to an editor that merely names uxplay.
    fake.processes[4242] = ["vim", "uxplay"]
    assert tools.mirroring_pid(system) is None
    assert not pid_file.exists()

    # A later uxplay under the same pid is another process.
    pid_file.write_text("4242 1000")
    pid_file.chmod(0o600)
    fake.processes[4242] = ["/usr/bin/uxplay", "-n", "desk"]
    fake.starts[4242] = 2000
    assert tools.mirroring_pid(system) is None
    assert fake.killed == []

    pid_file.write_text("4242 2000")
    pid_file.chmod(0o600)
    assert tools.mirroring_pid(system) == 4242
    assert tools.toggle_mirroring(system).ok and fake.killed == [(4242, signal.SIGTERM)]


def test_process_start_reads_this_process_and_ignores_missing_ones() -> None:
    assert isinstance(tools.process_start(os.getpid()), int)
    assert tools.process_start(2**22 + 12345) is None


def test_mirroring_is_greyed_out_without_uxplay_and_launch_errors_are_plain(tmp_path) -> None:
    fake = Fake(tmp_path)
    state = tools.mirror_state(fake.system())
    assert (state.installed, state.enabled) == (False, False)
    assert "UxPlay" in state.subtitle
    assert tools.toggle_mirroring(fake.system()).ok is False

    broken = Fake(tmp_path, desktop={tools.UXPLAY_DESKTOP_ID})
    broken.desktop[tools.UXPLAY_DESKTOP_ID].error = RuntimeError("GLib.Error: spawn failed")
    result = tools.toggle_mirroring(broken.system())
    assert result == tools.ActionResult(False, "Could not start UxPlay.")


# ---- LocalSend -------------------------------------------------------------------

def test_localsend_prefers_the_flatpak_entry_then_path(tmp_path) -> None:
    flatpak = Fake(tmp_path, installed={"localsend"}, desktop={tools.LOCALSEND_DESKTOP_ID})
    assert tools.start_localsend(flatpak.system()).ok
    assert flatpak.desktop[tools.LOCALSEND_DESKTOP_ID].launches == 1 and flatpak.commands == {}

    path = Fake(tmp_path, installed={"localsend"})
    assert tools.send_state(path.system()).enabled
    assert tools.start_localsend(path.system()).ok
    assert list(path.commands) == [("localsend",)]

    missing = Fake(tmp_path)
    state = tools.send_state(missing.system())
    assert (state.installed, state.enabled) == (False, False)
    assert tools.start_localsend(missing.system()).ok is False


def test_a_plugin_replacing_localsend_hides_the_app_and_explains(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"localsend", "uxplay"},
                desktop={tools.LOCALSEND_DESKTOP_ID})
    fake.replaced = frozenset({"localsend", "unknown-tool"})
    system = fake.system()
    keys = [tool.key for tool in tools.snapshot(system, tools.PhotoProbe()).tools]
    assert tools.SEND not in keys and tools.MIRROR in keys
    refused = tools.perform(system, tools.SEND)
    assert not refused.ok and "Send files" in refused.message
    assert fake.desktop[tools.LOCALSEND_DESKTOP_ID].launches == 0
    # A client that knows what is hidden passes it and reads no manifests.
    assert tools.perform(system, tools.SEND, hidden=frozenset()).ok
    assert fake.desktop[tools.LOCALSEND_DESKTOP_ID].launches == 1
    fake.replaced = frozenset({"uxplay"})
    assert "replaces" in tools.perform(fake.system(), tools.MIRROR).message


def test_replaced_by_plugins_reads_enabled_manifests_only(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    plugins = data / "blueferry" / "plugins"
    plugins.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (plugins / "io.example.ls.plugin").write_text(
        "[BlueFerry Plugin]\nId=io.example.ls\nName=LS\nVersion=1\nApiVersion=1.4\n"
        "MinBlueFerry=0.1\nCapabilities=share;\nHomepage=https://example.org\n"
        "Exec=ls-plugin serve\nReplacesTools=localsend;\n")
    assert tools.replaced_by_plugins() == frozenset({"localsend"})
    prefs = tmp_path / "config" / "blueferry" / "plugins.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"disabled": ["io.example.ls"]}')
    assert tools.replaced_by_plugins() == frozenset()


# ---- photos ----------------------------------------------------------------------

PHOTO_TOOLS = {"ifuse", "idevice_id", "idevicepair", "fusermount3"}
DEVICE = {("idevice_id", "-l"): CommandResult(0, "00008110-000A1B2C3D4E5F6G\n")}


def _photos_fake(tmp_path, answers=None) -> Fake:
    return Fake(tmp_path, installed=PHOTO_TOOLS, answers={**DEVICE, **(answers or {})})


def test_photos_need_ifuse_idevice_id_and_a_connected_device(tmp_path) -> None:
    missing = Fake(tmp_path, installed={"ifuse"})
    probe = tools.probe_photos(missing.system())
    assert probe == tools.PhotoProbe() and missing.ran == []
    photos, eject = tools.photos_states(probe)
    assert (photos.installed, photos.enabled, eject.enabled) == (False, False, False)

    empty = Fake(tmp_path, installed=PHOTO_TOOLS)
    photos, _eject = tools.photos_states(tools.probe_photos(empty.system()))
    assert (photos.installed, photos.enabled) == (True, False)
    assert "USB cable" in photos.subtitle

    present = _photos_fake(tmp_path)
    photos, eject = tools.photos_states(tools.probe_photos(present.system()))
    assert photos.enabled and not eject.enabled


def test_open_photos_mounts_privately_and_opens_dcim(tmp_path) -> None:
    fake = _photos_fake(tmp_path)
    system = fake.system()
    root = tools.photos_dir(system)

    def mount(argv):
        if argv[0] == "ifuse":
            fake.mounts.add(Path(argv[1]))
            (Path(argv[1]) / "DCIM").mkdir()

    fake.on_run = mount
    result = tools.open_photos(system)
    assert result.ok, result
    assert fake.ran == [["idevice_id", "-l"], ["idevicepair", "validate"], ["ifuse", str(root)]]
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert fake.opened == [(root / "DCIM").as_uri()]
    # Udid never reaches the user-facing text.
    assert "00008110" not in result.message

    # Second click: already mounted, only opens the folder again.
    fake.ran.clear()
    assert tools.open_photos(system).ok and fake.ran == []
    assert len(fake.opened) == 2

    ejected = tools.eject_photos(system)
    assert ejected.ok and fake.ran == [["fusermount3", "-u", str(root)]]


def test_untrusted_computer_asks_for_trust_and_pairing_can_be_started(tmp_path) -> None:
    fake = _photos_fake(tmp_path, {
        ("idevicepair", "validate"): CommandResult(1, "ERROR: Device 0000 is not paired"),
        ("idevicepair", "pair"): CommandResult(
            1, "ERROR: Please accept the trust dialog on the screen of device 0000"),
    })
    result = tools.open_photos(fake.system())
    assert result.needs_pairing and not result.ok
    assert "Trust This Computer" in result.message and "0000" not in result.message
    assert ["ifuse"] not in [argv[:1] for argv in fake.ran]

    paired = tools.pair_iphone(fake.system())
    assert paired.needs_pairing and "Trust This Computer" in paired.message
    fake.answers[("idevicepair", "pair")] = CommandResult(0, "SUCCESS: Paired with device 0000")
    assert tools.pair_iphone(fake.system()).ok


def test_mount_failures_and_timeouts_are_plain_text(tmp_path) -> None:
    fake = _photos_fake(tmp_path)
    system = fake.system()
    root = str(tools.photos_dir(system))
    fake.answers[("ifuse", root)] = CommandResult(1, "Failed to connect to lockdownd service")
    assert tools.open_photos(system).needs_pairing
    fake.answers[("ifuse", root)] = CommandResult(1, "fuse: device not found")
    assert "reconnect the cable" in tools.open_photos(system).message
    fake.answers[("ifuse", root)] = CommandResult(-1, timed_out=True)
    assert "did not answer in time" in tools.open_photos(system).message
    assert fake.opened == []

    fake.answers[("idevice_id", "-l")] = CommandResult(0, "")
    assert tools.open_photos(system).message == "No iPhone is connected over USB."


def test_eject_reports_busy_and_nothing_mounted(tmp_path) -> None:
    fake = _photos_fake(tmp_path)
    system = fake.system()
    assert tools.eject_photos(system).message == "No iPhone photos are mounted."
    root = tools.photos_dir(system)
    fake.mounts.add(root)
    fake.answers[("fusermount3", "-u", str(root))] = CommandResult(
        1, "fusermount3: failed to unmount: Device or resource busy")
    assert "still in use" in tools.eject_photos(system).message


def test_perform_and_snapshot_cover_every_entry(tmp_path) -> None:
    fake = _photos_fake(tmp_path)
    snap = tools.snapshot(fake.system(), tools.PhotoProbe(True, True, False))
    assert [tool.key for tool in snap.tools] == ["mirror", "send", "photos", "eject"]
    assert snap.get("photos").enabled and snap.get("nope") is None
    assert fake.ran == []  # a given probe is not repeated
    assert tools.perform(fake.system(), "bogus").ok is False
    assert tools.BLOCKING_ACTIONS == {"photos", "eject", "pair"}


# ---- real helpers, without starting tools ---------------------------------------

def test_run_command_times_out_and_reports_missing_programs(monkeypatch) -> None:
    def expired(*_args, **kwargs):
        assert kwargs["timeout"] == 3 and kwargs["stdin"] is subprocess.DEVNULL
        raise subprocess.TimeoutExpired("ifuse", 3)

    monkeypatch.setattr(tools.subprocess, "run", expired)
    assert tools.run_command(["ifuse", "/x"], 3) == CommandResult(-1, "", timed_out=True)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("ifuse")

    monkeypatch.setattr(tools.subprocess, "run", missing)
    assert tools.run_command(["ifuse"], 3).ok is False


def test_process_argv_reads_this_process_and_ignores_missing_ones() -> None:
    argv = tools.process_argv(os.getpid())
    assert argv and "python" in os.path.basename(argv[0])
    assert tools.process_argv(2**22 + 12345) is None


@pytest.mark.parametrize("action", tools.ACTIONS)
def test_every_action_has_a_handler(tmp_path, action) -> None:
    result = tools.perform(Fake(tmp_path).system(), action)
    assert isinstance(result, tools.ActionResult)


def test_a_hostname_never_turns_into_an_option(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    fake.hostname = "--help.lan"
    tools.perform(fake.system(), "mirror")
    assert list(fake.commands) == [("uxplay", "-n", "help", "-nh")]
    (tmp_path / "b").mkdir()
    fake = Fake(tmp_path / "b", installed={"uxplay"})
    fake.hostname = "-"
    tools.perform(fake.system(), "mirror")
    assert list(fake.commands) == [("uxplay", "-n", "BlueFerry", "-nh")]


def test_pidfd_terminate_checks_the_pinned_process_again(monkeypatch) -> None:


    sent: list[tuple[int, int]] = []
    closed: list[int] = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid: 77, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)),
                        raising=False)
    monkeypatch.setattr(os, "close", closed.append)
    tools.pidfd_terminate(4242, lambda pid: True)
    assert sent == [(77, signal.SIGTERM)] and closed == [77]
    with pytest.raises(ProcessLookupError):  # reused by another program meanwhile
        tools.pidfd_terminate(4242, lambda pid: False)
    assert sent == [(77, signal.SIGTERM)] and closed == [77, 77]
