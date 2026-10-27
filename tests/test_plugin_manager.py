"""Plugin installs from Git URLs with fake git and venv; no process, no network."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blueferry import plugin_manager
from blueferry.plugin_api.manifest import discover, parse_manifest
from blueferry.plugin_manager import InstallError, PluginManager, _rewrite
from blueferry.plugin_stopper import NOT_RUNNING, STOPPED, UNMANAGED, StopOutcome

URL = "https://git.example.org/me/blueferry-plugin-demo"
OLD, NEW, HEAD = "a" * 40, "b" * 40, "c" * 40
MANIFEST = """[BlueFerry Plugin]
Id=io.example.demo
Name=Demo photos
Version={version}
ApiVersion=1.1
MinBlueFerry=0.8
Capabilities=photos;
Source=https://git.example.org/me/blueferry-plugin-demo
Exec=blueferry-demo serve
Cli=blueferry-demo
Alias=demo

[Config url]
Label=Server URL
Type=url
Required=true
"""


class Symlink:
    """A symlink in a fake checkout (``FakeRunner.files`` value)."""

    def __init__(self, target) -> None:
        self.target = target


class FakeRunner:
    """Answers git, venv and pip like the real tools would, on the file system."""

    def __init__(self) -> None:
        self.tags = {"v0.1.0": OLD}
        self.head = HEAD
        self.files = {
            OLD: {"data/io.example.demo.plugin": MANIFEST.format(version="0.1.0")},
            NEW: {"data/io.example.demo.plugin": MANIFEST.format(version="0.2.0")},
            HEAD: {"src/demo/io.example.demo.plugin": MANIFEST.format(version="0.3.0.dev")},
        }
        self.calls: list[list[str]] = []
        self.checked_out: dict[str, str] = {}
        self.fail_pip = False
        self.programs = ("blueferry-demo",)

    def run(self, argv, *, cwd=None, timeout):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        assert timeout > 0
        if "ls-remote" in argv:
            if "--tags" in argv:
                return "".join(f"{commit}\trefs/tags/{tag}\n" for tag, commit in self.tags.items())
            return f"{self.head}\tHEAD\n" if self.head else ""
        if "clone" in argv:
            assert argv[-2] == URL and Path(argv[-1]).is_dir()
            return ""
        if "checkout" in argv:
            directory, commit = Path(argv[argv.index("-C") + 1]), argv[-1]
            for name, text in self.files[commit].items():
                (directory / name).parent.mkdir(parents=True, exist_ok=True)
                if isinstance(text, Symlink):
                    (directory / name).symlink_to(text.target)
                else:
                    (directory / name).write_text(text)
            self.checked_out[str(directory)] = commit
            return ""
        if "rev-parse" in argv:
            return self.checked_out[argv[argv.index("-C") + 1]] + "\n"
        if "log" in argv:
            return "bbbbbbb Add videos\nccccccc Fix paging\n"
        if argv[1:3] == ["-m", "venv"]:
            assert "--system-site-packages" in argv
            (Path(argv[-1]) / "bin").mkdir(parents=True)
            (Path(argv[-1]) / "bin" / "python").write_text("")
            return ""
        if argv[1:3] == ["-m", "pip"]:
            if self.fail_pip:
                raise InstallError("pip failed: no matching distribution")
            for program in self.programs:
                (Path(argv[0]).parent / program).write_text("")
            return ""
        if argv[1] == "-c":
            return ""
        raise AssertionError(f"unexpected command {argv}")


class FakeStopper:
    """Records which bus name and venvs a stop was asked for; no bus, no signal."""

    def __init__(self, state: str = NOT_RUNNING) -> None:
        self.state = state
        self.calls: list[tuple[str, list[Path]]] = []

    def __call__(self, bus_name, venvs):
        self.calls.append((bus_name, list(venvs)))
        return StopOutcome(self.state, 4242 if self.state != NOT_RUNNING else 0)


@pytest.fixture
def setup(tmp_path):
    runner = FakeRunner()
    manager = PluginManager(
        data_home=tmp_path / "data", settings_path=tmp_path / "config" / "plugins.json",
        runner=runner, python="/usr/bin/python3", clock=lambda: 1000.0,
        stopper=FakeStopper(),
    )
    return manager, runner


def _ran(runner, word):
    return [call for call in runner.calls if word in call]


@pytest.mark.parametrize("prepare_before_install", [False, True])
def test_colliding_ids_cannot_replace_activation(setup, prepare_before_install):
    manager, runner = setup
    runner.files[OLD] = {"demo.plugin": MANIFEST.format(version="1").replace(
        "Id=io.example.demo", "Id=io.example.demo-one")}
    first = manager.prepare(URL)
    runner.files[OLD] = {"demo.plugin": MANIFEST.format(version="1").replace(
        "Id=io.example.demo", "Id=io.example.demo_one")}
    second = manager.prepare(URL) if prepare_before_install else None
    record = manager.commit(first)
    service = manager.service_path(first.manifest)
    original = service.read_bytes()
    calls = len(runner.calls)
    with pytest.raises(InstallError, match="shares its D-Bus name"):
        manager.commit(second) if second else manager.prepare(URL)
    assert service.read_bytes() == original
    assert manager.records() == {record.id: record}
    assert Path(record.venv).is_dir()
    assert not any("pip" in call for call in runner.calls[calls:])
    if second:
        manager.discard(second)


def test_colliding_unmanaged_manifest_blocks_install(setup):
    manager, _runner = setup
    manager.root.mkdir(parents=True)
    for plugin_id in ("io.example.demo-one", "io.example.demo_one"):
        manager.manifest_path(plugin_id).write_text(MANIFEST.format(version="1").replace(
            "Id=io.example.demo", f"Id={plugin_id}"))
    # Even an ID already present in an ambiguous manual installation must
    # not publish a new activation file over the other ID's service.
    _runner.files[OLD] = {"demo.plugin": MANIFEST.format(version="1").replace(
        "Id=io.example.demo", "Id=io.example.demo-one")}
    with pytest.raises(InstallError, match="shares its D-Bus name"):
        manager.prepare(URL)
    assert not _ran(_runner, "pip")


@pytest.mark.parametrize("change", ["update", "reinstall", "remove"])
def test_stale_confirmation_preserves_current_install(setup, change):
    manager, runner = setup
    manager.commit(manager.prepare(URL))
    runner.tags["v0.2.0"] = NEW
    waiting = manager.prepare_update("io.example.demo")
    assert waiting is not None
    if change == "remove":
        manager.remove("io.example.demo")
    else:
        ref = "v0.1.0" if change == "reinstall" else HEAD
        manager.commit(manager.prepare(URL, ref))
    current = manager.records()
    service = manager.service_path(waiting.manifest)
    before = service.read_bytes() if service.exists() else None
    calls = len(runner.calls)
    stops = len(manager.stopper.calls)
    with pytest.raises(InstallError, match="changed since preparation"):
        manager.commit(waiting)
    assert manager.records() == current
    assert (service.read_bytes() if service.exists() else None) == before
    assert len(runner.calls) == calls and len(manager.stopper.calls) == stops
    assert all(Path(record.venv).is_dir() for record in current.values())
    manager.discard(waiting)


def test_two_first_install_confirmations_cannot_overwrite_each_other(setup):
    manager, _runner = setup
    first, second = manager.prepare(URL), manager.prepare(URL)
    record = manager.commit(first)
    with pytest.raises(InstallError, match="changed since preparation"):
        manager.commit(second)
    assert manager.records()[record.id] == record
    manager.discard(second)


def test_prepare_runs_no_plugin_code_and_commit_publishes_it(setup, monkeypatch) -> None:
    manager, runner = setup
    monkeypatch.setenv("XDG_DATA_HOME", str(manager.data_home))
    prepared = manager.prepare(URL)
    assert (prepared.ref, prepared.commit) == ("v0.1.0", OLD)
    assert not _ran(runner, "venv") and not _ran(runner, "pip")
    rows = dict(prepared.summary())
    assert rows["Source"] == URL and rows["Ref"].startswith("v0.1.0 (commit aaaaaaaaaaaa")
    assert rows["Capabilities"] == "photos" and rows["Settings"] == "Server URL"
    assert rows["Runs"] == "blueferry-demo serve"

    record = manager.commit(prepared)
    venv = manager.root / "venvs" / "io.example.demo-aaaaaaaaaaaa"
    assert record.venv == str(venv) and record.ref == "v0.1.0"
    installed = parse_manifest(manager.manifest_path("io.example.demo").read_text())
    assert installed.exec == (str(venv / "bin" / "blueferry-demo"), "serve")
    assert installed.cli == (str(venv / "bin" / "blueferry-demo"),)
    assert [field.key for field in installed.config] == ["url"]
    service = manager.service_path(installed).read_text()
    assert f"Exec={venv}/bin/blueferry-demo serve" in service
    assert os.stat(manager.manifest_path("io.example.demo")).st_mode & 0o022 == 0
    assert (manager.source_dir("io.example.demo") / "data").is_dir()
    assert not list((manager.root / "src").glob(".staging-*"))
    assert manager.records()["io.example.demo"] == record
    entries, ignored = manager.entries()
    assert ignored == [] and entries[0].managed and entries[0].source == URL


def test_without_tags_the_default_branch_is_pinned_as_a_commit(setup) -> None:
    manager, runner = setup
    runner.tags = {"latest": OLD}  # not a version tag
    prepared = manager.prepare(URL)
    assert prepared.ref == prepared.commit == HEAD
    assert prepared.manifest.version == "0.3.0.dev"
    manager.discard(prepared)
    assert not prepared.staging.exists()


@pytest.mark.parametrize("url", [
    "http://git.example.org/x", "git@github.com:me/x.git", "file:///etc", "ext::sh -c id",
    "https://git.example.org/../x", "https://exa mple.org/x",
])
def test_only_https_git_urls(setup, url) -> None:
    manager, runner = setup
    with pytest.raises(InstallError, match="https"):
        manager.prepare(url)
    assert runner.calls == []


def test_explicit_refs(setup) -> None:
    manager, runner = setup
    runner.tags["v0.2.0"] = NEW
    assert manager.resolve(URL, "v0.1.0") == ("v0.1.0", OLD)
    assert manager.resolve(URL, NEW) == (NEW, NEW)
    assert manager.resolve(URL) == ("v0.2.0", NEW)
    with pytest.raises(InstallError, match="no tag"):
        manager.resolve(URL, "v9")
    with pytest.raises(InstallError, match="tag or a full"):
        manager.resolve(URL, "--upload-pack=x")


def test_a_manifest_that_runs_something_else_is_refused_before_asking(setup) -> None:
    manager, runner = setup
    runner.files[OLD] = {"data/x.plugin": MANIFEST.format(version="1").replace(
        "Exec=blueferry-demo serve", "Exec=/bin/sh -c evil")}
    with pytest.raises(InstallError, match="must name a program"):
        manager.prepare(URL)
    runner.files[OLD] = {"a.plugin": MANIFEST.format(version="1"),
                         "data/b.plugin": MANIFEST.format(version="1")}
    prepared = manager.prepare(URL)
    assert prepared.manifest.version == "1"
    manager.discard(prepared)
    runner.files[OLD] = {"README.md": "no manifest"}
    with pytest.raises(InstallError, match="exactly one"):
        manager.prepare(URL)
    assert not list((manager.root / "src").glob(".staging-*"))


def test_a_failed_install_restores_the_previous_one(setup) -> None:
    manager, runner = setup
    manager.commit(manager.prepare(URL))
    before = manager.manifest_path("io.example.demo").read_text()
    runner.tags["v0.2.0"] = NEW
    runner.fail_pip = True
    with pytest.raises(InstallError, match="pip failed"):
        manager.commit(manager.prepare(URL))
    assert manager.manifest_path("io.example.demo").read_text() == before
    assert manager.records()["io.example.demo"].commit == OLD
    assert (manager.source_dir("io.example.demo") / "data" / "io.example.demo.plugin").read_text() \
        == MANIFEST.format(version="0.1.0")
    assert not (manager.root / "venvs" / "io.example.demo-bbbbbbbbbbbb").exists()


def test_a_failed_reinstall_preserves_the_working_runtime(setup) -> None:
    manager, runner = setup
    original = manager.commit(manager.prepare(URL))
    program = Path(original.venv) / "bin" / "blueferry-demo"
    program.write_text("working installation")
    manifest = manager.manifest_path(original.id)
    service = manager.service_path(parse_manifest(manifest.read_text()))
    before = manifest.read_text(), service.read_text()
    prepared = manager.prepare(URL)
    runner.fail_pip = True
    with pytest.raises(InstallError, match="pip failed"):
        manager.commit(prepared)
    assert program.read_text() == "working installation"
    assert (manifest.read_text(), service.read_text()) == before
    assert manager.records()[original.id] == original
    assert manager.stopper.calls == []
    assert list((manager.root / "venvs").iterdir()) == [Path(original.venv)]


@pytest.mark.parametrize("publication", ["service", "record"])
def test_a_failed_reinstall_publication_restores_all_metadata(setup, monkeypatch, publication) -> None:
    manager, _runner = setup
    original = manager.commit(manager.prepare(URL))
    program = Path(original.venv) / "bin" / "blueferry-demo"
    program.write_text("working installation")
    manifest = manager.manifest_path(original.id)
    service = manager.service_path(parse_manifest(manifest.read_text()))
    before = manifest.read_text(), service.read_text()
    write = plugin_manager._write_text
    failing_path = service if publication == "service" else manager.root / "installs.json"

    def fail(path, text, mode=0o644):
        if path == failing_path:
            raise OSError("publication failed")
        return write(path, text, mode)

    monkeypatch.setattr(plugin_manager, "_write_text", fail)
    with pytest.raises(OSError, match="publication failed"):
        manager.commit(manager.prepare(URL))
    assert program.read_text() == "working installation"
    assert (manifest.read_text(), service.read_text()) == before
    assert manager.records()[original.id] == original
    assert manager.stopper.calls == []
    assert list((manager.root / "venvs").iterdir()) == [Path(original.venv)]


@pytest.mark.parametrize("operation", ["install", "update", "remove"])
def test_cleanup_does_not_delete_another_plugin_with_a_similar_id(setup, operation) -> None:
    manager, runner = setup
    original_files = runner.files[OLD]
    runner.files[OLD] = {"data/extra.plugin": MANIFEST.format(version="0.1.0").replace(
        "Id=io.example.demo", "Id=io.example.demo-extra")}
    other = manager.commit(manager.prepare(URL))
    runner.files[OLD] = original_files
    manager.commit(manager.prepare(URL))
    if operation == "update":
        runner.tags["v0.2.0"] = NEW
        manager.commit(manager.prepare_update("io.example.demo"))
    elif operation == "remove":
        manager.remove("io.example.demo")
    assert (Path(other.venv) / "bin" / "blueferry-demo").is_file()
    assert manager.records()[other.id] == other
    assert all(Path(other.venv) not in paths for _name, paths in manager.stopper.calls)


def test_update_shows_the_change_and_replaces_the_venv(setup) -> None:
    manager, runner = setup
    manager.commit(manager.prepare(URL))
    assert manager.prepare_update("io.example.demo") is None
    runner.tags["v0.2.0"] = NEW
    record, ref, commit = manager.check_update("io.example.demo")
    assert (record.commit, ref, commit) == (OLD, "v0.2.0", NEW)
    prepared = manager.prepare_update("io.example.demo")
    assert prepared is not None and prepared.changes == ["bbbbbbb Add videos", "ccccccc Fix paging"]
    assert f"{OLD}..{NEW}" in _ran(runner, "log")[0]
    assert dict(prepared.summary())["Replaces"].startswith("v0.1.0")
    manager.commit(prepared)
    assert [path.name for path in (manager.root / "venvs").iterdir()] == [
        "io.example.demo-bbbbbbbbbbbb"]
    with pytest.raises(InstallError, match="not installed with"):
        manager.check_update("io.example.other")


def test_remove_deletes_only_what_install_wrote(setup, tmp_path) -> None:
    manager, _runner = setup
    manager.commit(manager.prepare(URL))
    plugin_settings = tmp_path / "config" / "blueferry" / "plugins" / "io.example.demo"
    plugin_settings.mkdir(parents=True)
    manager.set_enabled("io.example.demo", False)
    service = manager.service_path(parse_manifest(MANIFEST.format(version="1")))
    removed = manager.remove("io.example.demo").paths
    assert manager.manifest_path("io.example.demo") in removed and service in removed
    assert not service.exists() and not list((manager.root / "venvs").iterdir())
    assert manager.records() == {} and plugin_settings.is_dir()
    assert manager.disabled() == frozenset()
    with pytest.raises(InstallError):
        manager.remove("io.example.demo")


def test_enable_and_disable_persist(setup) -> None:
    manager, _runner = setup
    manager.set_enabled("io.a.b", False)
    manager.set_enabled("io.c.d", False)
    manager.set_enabled("io.a.b", True)
    assert manager.disabled() == frozenset({"io.c.d"})
    assert os.stat(manager.settings_path).st_mode & 0o077 == 0


def test_install_replaces_a_manually_installed_manifest(setup, monkeypatch) -> None:
    """The pre-store Immich setup wrote its own manifest; install takes it over."""
    manager, _runner = setup
    monkeypatch.setenv("XDG_DATA_HOME", str(manager.data_home))
    manager.root.mkdir(parents=True)
    old = manager.manifest_path("io.example.demo")
    old.write_text(MANIFEST.format(version="0.0.1").replace(
        "Exec=blueferry-demo serve", "Exec=/home/me/.local/bin/blueferry-demo serve"))
    old.chmod(0o644)
    prepared = manager.prepare(URL)
    assert prepared.unmanaged is not None and prepared.unmanaged.version == "0.0.1"
    assert "manually installed" in dict(prepared.summary())["Replaces"]
    manager.commit(prepared)
    assert discover([manager.root]).plugins[0].version == "0.1.0"


def test_rewrite_touches_only_the_plugin_group() -> None:
    text = "[BlueFerry Plugin]\nExec=a serve\nCli=a\n[Config x]\nExec=keep\n"
    assert _rewrite(text, ("/v/a", "serve"), ("/v/a",)) == (
        "[BlueFerry Plugin]\nExec=/v/a serve\nCli=/v/a\n[Config x]\nExec=keep\n"
    )


def test_records_survive_garbage(setup) -> None:
    manager, _runner = setup
    manager.root.mkdir(parents=True)
    (manager.root / "installs.json").write_text(json.dumps({"plugins": {"x": {"bad": 1}}}))
    assert manager.records() == {}


BUS = "io.weirdware.BlueFerry.Plugin.io.example.demo"


def test_update_remove_and_disable_stop_the_old_process(setup) -> None:
    manager, runner = setup
    stopper = manager.stopper
    stopper.state = STOPPED
    prepared = manager.prepare(URL)
    manager.commit(prepared)
    assert stopper.calls == [] and prepared.stop is None  # first install: nothing to stop
    old = manager.root / "venvs" / "io.example.demo-aaaaaaaaaaaa"

    runner.tags["v0.2.0"] = NEW
    prepared = manager.prepare_update("io.example.demo")
    manager.commit(prepared)
    assert stopper.calls == [(BUS, [old])] and prepared.stop.state == STOPPED
    new = manager.root / "venvs" / "io.example.demo-bbbbbbbbbbbb"

    assert manager.set_enabled("io.example.demo", False).state == STOPPED
    assert stopper.calls[-1] == (BUS, [new])
    assert manager.set_enabled("io.example.demo", True) is None
    # Not installed by BlueFerry: no venv to check a process against.
    assert manager.set_enabled("io.unmanaged.x", False).state == UNMANAGED
    assert len(stopper.calls) == 2

    removal = manager.remove("io.example.demo")
    assert removal.stop.state == STOPPED and stopper.calls[-1] == (BUS, [new])


def test_reinstalling_the_same_commit_stops_the_process_of_the_rebuilt_venv(setup) -> None:
    manager, _runner = setup
    manager.commit(manager.prepare(URL))
    prepared = manager.prepare(URL)
    replacement = manager.commit(prepared)
    venv = manager.root / "venvs" / "io.example.demo-aaaaaaaaaaaa"
    assert manager.stopper.calls == [(BUS, [venv])] and prepared.stop.state == NOT_RUNNING
    assert Path(replacement.venv) != venv
    assert (Path(replacement.venv) / "bin" / "blueferry-demo").is_file()
    assert not venv.exists()
    assert manager.records()[replacement.id] == replacement
    manager.remove(replacement.id)
    assert not Path(replacement.venv).exists()


def test_a_failed_update_stops_nothing(setup) -> None:
    manager, runner = setup
    manager.commit(manager.prepare(URL))
    runner.tags["v0.2.0"] = NEW
    runner.fail_pip = True
    prepared = manager.prepare_update("io.example.demo")
    with pytest.raises(InstallError):
        manager.commit(prepared)
    assert manager.stopper.calls == []


def test_a_symlinked_or_oversized_manifest_is_refused(setup, tmp_path) -> None:
    manager, runner = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.plugin").write_text(MANIFEST.format(version="9"))
    for files in (
        {"data/io.example.demo.plugin": Symlink(outside / "x.plugin")},
        {"data": Symlink(outside)},  # a symlinked folder on the way
        {"src/demo": Symlink(outside)},
    ):
        runner.files[OLD] = files
        with pytest.raises(InstallError, match="regular file"):
            manager.prepare(URL)
    runner.files[OLD] = {"data/io.example.demo.plugin": MANIFEST.format(version="1")
                         + "# " + "x" * (16 * 1024) + "\n"}
    with pytest.raises(InstallError, match="too large"):
        manager.prepare(URL)
    assert not list((manager.root / "src").glob(".staging-*"))


def _write_records(manager, plugins) -> None:
    manager.root.mkdir(parents=True, exist_ok=True)
    (manager.root / "installs.json").write_text(json.dumps({"plugins": plugins}))


def _record(manager, **changes):
    value = {"id": "io.example.demo", "url": URL, "ref": "v0.1.0", "commit": OLD,
             "venv": str(manager.root / "venvs" / "io.example.demo-aaaaaaaaaaaa"),
             "installed_at": 1.0}
    value.update(changes)
    return value


@pytest.mark.parametrize("changes", [
    {"id": "../../etc"},
    {"id": "io.example.other"},  # key and id disagree
    {"commit": "--upload-pack=evil" + "a" * 22},
    {"commit": "A" * 40},
    {"ref": "../../x"},
    {"ref": "-x"},
    {"url": "http://git.example.org/x"},
    {"venv": "/home"},
    {"venv": "{root}/venvs/io.example.demo-aaaaaaaaaaaa/../../.."},
    {"venv": "{root}/venvs/io.example.other-aaaaaaaaaaaa"},
    {"installed_at": "yesterday"},
    {"ref": 7},
])
def test_installs_json_entries_that_point_elsewhere_are_ignored(setup, changes) -> None:
    manager, _runner = setup
    changes = {key: value.replace("{root}", str(manager.root)) if isinstance(value, str)
               else value for key, value in changes.items()}
    _write_records(manager, {"io.example.demo": _record(manager, **changes)})
    assert manager.records() == {}
    with pytest.raises(InstallError, match="not installed with"):
        manager.remove("io.example.demo")
    assert manager.set_enabled("io.example.demo", False).state == UNMANAGED
    assert manager.stopper.calls == []


def test_a_valid_installs_json_entry_is_read(setup) -> None:
    manager, _runner = setup
    _write_records(manager, {"io.example.demo": _record(manager)})
    assert manager.records()["io.example.demo"].commit == OLD
    _write_records(manager, [1, 2])
    assert manager.records() == {}


def test_check_update_never_downgrades(setup) -> None:
    manager, runner = setup
    runner.tags = {"v0.1.0": OLD, "v0.2.0": NEW}
    manager.commit(manager.prepare(URL, "v0.2.0"))
    # The newer tag disappears upstream (or someone retags an older one).
    runner.tags = {"v0.1.0": OLD, "v0.2.0": NEW, "v0.1.9": HEAD}
    record, ref, commit = manager.check_update("io.example.demo")
    assert (ref, commit) == ("v0.2.0", NEW)
    runner.tags = {"v0.1.0": OLD}
    record, ref, commit = manager.check_update("io.example.demo")
    assert (ref, commit) == ("v0.2.0", record.commit)
    assert manager.prepare_update("io.example.demo") is None


def test_a_second_change_waits_for_the_lock_and_then_gives_up(setup, monkeypatch) -> None:
    import fcntl

    manager, runner = setup
    monkeypatch.setattr(plugin_manager, "LOCK_WAIT_SEC", 0.2)
    manager.root.mkdir(parents=True)
    other = os.open(manager.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(other, fcntl.LOCK_EX)  # another process installs right now
        for change in (lambda: manager.prepare(URL),
                       lambda: manager.set_enabled("io.example.demo", False),
                       lambda: manager.update_settings(indexes=[])):
            with pytest.raises(InstallError, match="another plugin install"):
                change()
        assert runner.calls == []
        fcntl.flock(other, fcntl.LOCK_UN)
        manager.commit(manager.prepare(URL))  # free again; nested locking is fine
        manager.remove("io.example.demo")
    finally:
        os.close(other)
    assert os.stat(manager.root / ".lock").st_mode & 0o077 == 0


def test_git_and_pip_errors_reach_the_clients_as_one_plain_line(monkeypatch) -> None:
    import subprocess

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "", "warning\n\x1b[31mERROR:\x1b[0m <b>no</b>‮ matching\tdist\n")

    monkeypatch.setattr(plugin_manager.subprocess, "run", fake_run)
    with pytest.raises(InstallError) as failure:
        plugin_manager.SubprocessRunner().run(["/venv/bin/pip", "install"], timeout=1)
    assert str(failure.value) == "pip failed: [31mERROR: [0m <b>no</b> matching dist"
