"""Contact photo surfaces: operations, rate limits, popups, daemon wiring, CLI."""
from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from blueferry import cli_contacts, config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.cli import app
from blueferry.client import BackendClient
from blueferry.contact_repository import ContactRepository
from blueferry.dbus_security import CallerGuard
from blueferry.errors import InvalidArgumentsError, NotReadyError, RateLimitError
from blueferry.event_dispatcher import EventDispatcher
from blueferry.limits import MAX_CONTACT_ADDRESS_CHARS, MAX_CONTACT_PHOTO_BYTES
from blueferry.sinks import libnotify as libnotify_mod
from blueferry.sinks.libnotify import LibnotifySink

from .photo_fixtures import jpeg, png

JPEG = jpeg()
PNG = png()


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error):
        pass


class _Contacts:
    def __init__(self, photos: dict[str, bytes]) -> None:
        self.photos = photos
        self.calls: list[str] = []

    def photo(self, address):
        self.calls.append(address)
        return self.photos.get(address)


# ---- operations --------------------------------------------------------------

def test_disabled_photos_are_inert_even_without_a_contact_cache() -> None:
    contacts = _Contacts({"+15551112222": JPEG})
    operations = BackendOperations(_Sessions(), BackendDependencies(contacts=contacts))
    assert operations.contact_photo("+15551112222") == b""
    assert contacts.calls == []
    assert BackendOperations(_Sessions()).contact_photo("+15551112222") == b""
    assert operations.status()["contact_photos"] is False


def test_enabled_photos_return_bytes_or_empty() -> None:
    contacts = _Contacts({"+15551112222": JPEG})
    operations = BackendOperations(
        _Sessions(), BackendDependencies(contacts=contacts, contact_photos=True),
    )
    assert operations.contact_photo(" +15551112222 ") == JPEG
    assert operations.contact_photo("+19999999999") == b""
    assert operations.status()["contact_photos"] is True
    with pytest.raises(NotReadyError):
        BackendOperations(
            _Sessions(), BackendDependencies(contact_photos=True),
        ).contact_photo("+15551112222")


def test_busy_photo_store_is_reported_as_retryable_not_ready() -> None:
    from blueferry.contact_repository import PhotoStoreBusy

    class _Busy:
        def photo(self, _address):
            raise PhotoStoreBusy("locked")

    operations = BackendOperations(
        _Sessions(), BackendDependencies(contacts=_Busy(), contact_photos=True),
    )
    with pytest.raises(NotReadyError, match="retry"):
        operations.contact_photo("+15551112222")


@pytest.mark.parametrize("address", ["", "   ", "x" * (MAX_CONTACT_ADDRESS_CHARS + 1)])
def test_photo_address_is_validated_before_any_lookup(address) -> None:
    contacts = _Contacts({})
    operations = BackendOperations(
        _Sessions(), BackendDependencies(contacts=contacts, contact_photos=True),
    )
    with pytest.raises(InvalidArgumentsError):
        operations.contact_photo(address)
    assert contacts.calls == []


def test_photo_lookups_have_their_own_rate_limit() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )
    for _ in range(120):
        guard.authorize(":1.20", "contact-photo")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "contact-photo")
    # One client's full quota leaves room for other clients' avatar lists.
    for _ in range(120):
        guard.authorize(":1.21", "contact-photo")
    # ... but the daemon-wide window (600/min) still bounds all of them.
    for caller in range(22, 25):
        for _ in range(120):
            guard.authorize(f":1.{caller}", "contact-photo")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.30", "contact-photo")
    # Photo floods do not starve ordinary reads.
    guard.authorize(":1.20", "read")
    now[0] += 61
    guard.authorize(":1.21", "contact-photo")


# ---- client --------------------------------------------------------------------

class _Interface:
    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.calls = []

    def GetStatus(self, **_kwargs):
        return '{"api_version": 2}'

    def GetContactPhoto(self, address, **kwargs):
        self.calls.append((address, kwargs))
        return self.reply


@pytest.mark.parametrize("reply,expected", [
    (JPEG, JPEG),
    (b"", b""),
    (b"GIF89a" + b"\x00" * 8, b""),
    (jpeg(padding=MAX_CONTACT_PHOTO_BYTES), b""),
    (jpeg(30_000, 30_000), b""),
])
def test_client_rejects_replies_outside_the_protocol_bounds(reply, expected) -> None:
    interface = _Interface(reply)
    client = BackendClient(interface_factory=lambda _name: interface)
    assert client.contact_photo("+15551112222") == expected
    assert interface.calls[0][1]["byte_arrays"] is True


# ---- desktop popups --------------------------------------------------------------

class _Notifications:
    def __init__(self) -> None:
        self.calls = []

    def Notify(self, *args):
        self.calls.append(args)
        return 1


def _sink(monkeypatch, contact_photo=None):
    monkeypatch.setattr(libnotify_mod, "get_session_bus", lambda: SimpleNamespace(list_names=lambda: []))
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _Notifications()
    sink._pending = {}
    sink._msg_subs = {}
    if contact_photo is not None:
        sink._contact_photo = contact_photo
    return sink


def _event():
    return SimpleNamespace(
        kind="sms_received", display_sender="Alice", body="Hi",
        message_path=None, sender_address="+15551112222",
    )


def test_popup_uses_the_volatile_avatar_as_image_path(monkeypatch, tmp_path) -> None:
    avatar = tmp_path / "avatar-x.jpg"
    requested = []
    sink = _sink(monkeypatch, lambda address: requested.append(address) or str(avatar))
    sink.handle(_event())
    hints = sink._notif.calls[0][6]
    assert hints["image-path"] == avatar.as_uri()
    assert requested == ["+15551112222"]
    assert sink._notif.calls[0][2] == "phone-symbolic"


def test_popup_without_photo_or_when_disabled_has_no_image_hint(monkeypatch) -> None:
    for provider in (None, lambda _address: None):
        sink = _sink(monkeypatch, provider)
        sink.handle(_event())
        assert "image-path" not in sink._notif.calls[0][6]

    def broken(_address):
        raise RuntimeError("locked")

    sink = _sink(monkeypatch, broken)
    sink.handle(_event())
    assert len(sink._notif.calls) == 1 and "image-path" not in sink._notif.calls[0][6]


def test_dispatcher_passes_the_photo_provider_only_when_enabled(monkeypatch) -> None:
    from blueferry import event_dispatcher

    class _Sqlite:
        name = "sqlite"

        def __init__(self, **_kwargs):
            pass

    class _Bus:
        def add_signal_receiver(self, *_args, **_kwargs):
            return SimpleNamespace(remove=lambda: None)

    monkeypatch.setattr(event_dispatcher, "SqliteSink", _Sqlite)
    received = []

    def legacy_factory(*, defer_mark_read, notification_policy,
                       contacts_only_notifications, on_open_message):
        received.append(None)
        return SimpleNamespace(name="libnotify")

    def factory(**kwargs):
        received.append(kwargs.get("contact_photo"))
        return SimpleNamespace(name="libnotify")

    # Disabled: the exact historical keyword set, so existing sinks still work.
    EventDispatcher(
        object(), defer_mark_read=lambda _path: None,
        notification_sink_factory=legacy_factory, session_bus=_Bus(),
    ).setup()
    provider = lambda _address: None  # noqa: E731
    EventDispatcher(
        object(), defer_mark_read=lambda _path: None, contact_photo=provider,
        notification_sink_factory=factory, session_bus=_Bus(),
    ).setup()
    assert received == [None, provider]


# ---- daemon wiring ---------------------------------------------------------------

class _Wallet:
    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
        return b"K" * 32

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        return True


def _seed_photo() -> None:
    from blueferry.settings_store import SettingsStore
    from blueferry.storage_security import StorageSecurity

    storage = StorageSecurity(settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet())
    try:
        ContactRepository(storage).replace([("Alice", ["15551112222"], [])], photos=[JPEG])
    finally:
        storage.close()


def _photo_rows() -> int:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(config.CONTACTS_DB)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM contact_photos").fetchone()[0])


def test_disabled_daemon_erases_old_photos_and_wires_nothing(make_daemon, monkeypatch) -> None:
    _seed_photo()
    assert _photo_rows() == 1
    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    daemon = make_daemon()
    assert _photo_rows() == 0
    assert daemon.photo_files is None
    assert daemon.events.contact_photo is None
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})
    status = daemon._status()
    assert status["contact_photos"] is False
    assert "contact_photo_revision" not in status and "contact_photo_count" not in status


def test_disabled_daemon_never_writes_a_profile_without_photos(make_daemon, monkeypatch) -> None:
    from blueferry.settings_store import SettingsStore
    from blueferry.storage_security import StorageSecurity

    storage = StorageSecurity(settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet())
    try:
        ContactRepository(storage).replace([("Alice", ["15551112222"], [])])
    finally:
        storage.close()
    before = config.CONTACTS_DB.read_bytes()
    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    make_daemon()
    assert config.CONTACTS_DB.read_bytes() == before
    assert not Path(f"{config.CONTACTS_DB}-journal").exists()


def test_enabled_daemon_keeps_photos_and_reports_content_free_status(
    make_daemon, monkeypatch,
) -> None:
    _seed_photo()
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    daemon = make_daemon()
    assert _photo_rows() == 1
    assert daemon.photo_files is not None
    assert daemon.events.contact_photo == daemon.photo_files.path_for
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})
    status = daemon._status()
    assert status["contact_photos"] is True
    assert isinstance(status["contact_photo_revision"], int)
    assert status["contact_photo_count"] == 0  # storage not yet prepared/unlocked


def test_contact_refresh_retires_volatile_avatar_files(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    daemon = make_daemon()
    calls = []
    daemon.photo_files = SimpleNamespace(
        clear=lambda: calls.append("clear"), retire=lambda: calls.append("retire"),
    )
    monkeypatch.setattr(daemon, "_emit_status", lambda: None)
    monkeypatch.setattr(daemon, "_mark_setup_task", lambda *_args: None)
    daemon._contacts_refreshed()
    # Shown popups may still read their icon; storage changes still clear.
    assert calls == ["retire"]
    monkeypatch.setattr(daemon.contact_sync, "storage_changed", lambda: None)
    daemon._on_storage_changed()
    assert calls == ["retire", "clear"]


# ---- CLI -----------------------------------------------------------------------

class _CliBackend:
    def __init__(self) -> None:
        self.photos: dict[str, bytes] = {}
        self.matches: list[tuple[str, str]] = []
        self.enabled = True

    def status(self):
        return SimpleNamespace(to_dict=lambda: {"contact_photos": self.enabled})

    def find_contacts(self, _query):
        return list(self.matches)

    def contact_photo(self, address):
        return self.photos.get(address, b"")


@pytest.fixture
def cli_backend(monkeypatch):
    backend = _CliBackend()
    backend.photos = {"alice@example.com": PNG}
    backend.matches = [("Alice", "+15550000000"), ("Alice", "alice@example.com")]
    backend.enabled = True
    monkeypatch.setattr(cli_contacts, "BackendClient", lambda: backend)
    return backend


def test_cli_writes_an_owner_only_file_and_refuses_to_overwrite(cli_backend, tmp_path) -> None:
    target = tmp_path / "alice.png"
    runner = CliRunner()
    result = runner.invoke(app, ["contacts-photo", "Alice", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == PNG
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "PNG" in result.output

    again = runner.invoke(app, ["contacts-photo", "Alice", "-o", str(target)])
    assert again.exit_code == 2
    target.write_bytes(b"old")
    forced = runner.invoke(app, ["contacts-photo", "Alice", "-o", str(target), "--force"])
    assert forced.exit_code == 0 and target.read_bytes() == PNG


def test_cli_does_not_follow_a_symlinked_output(cli_backend, tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = tmp_path / "link.png"
    link.symlink_to(victim)
    result = CliRunner().invoke(app, ["contacts-photo", "Alice", "-o", str(link), "--force"])
    assert result.exit_code == 2
    assert victim.read_bytes() == b"keep"


def test_cli_force_never_truncates_a_fifo_or_device(cli_backend, tmp_path) -> None:
    import os

    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    # Without a reader the non-blocking open fails at once instead of hanging.
    result = CliRunner().invoke(app, ["contacts-photo", "Alice", "-o", str(fifo), "--force"])
    assert result.exit_code == 2
    # With a reader attached the open succeeds, but fstat rejects the pipe
    # before anything is written.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = CliRunner().invoke(
            app, ["contacts-photo", "Alice", "-o", str(fifo), "--force"],
        )
        assert result.exit_code == 2 and "regular file" in result.output
        assert os.read(reader, 16) == b""
    finally:
        os.close(reader)
    result = CliRunner().invoke(app, ["contacts-photo", "Alice", "-o", "/dev/null", "--force"])
    assert result.exit_code == 2 and "regular file" in result.output


def test_cli_force_tightens_an_existing_files_mode(cli_backend, tmp_path) -> None:
    target = tmp_path / "shared.png"
    target.write_bytes(b"old contents that are longer than the photo" * 10)
    target.chmod(0o644)
    result = CliRunner().invoke(app, ["contacts-photo", "Alice", "-o", str(target), "--force"])
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == PNG
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_cli_reports_disabled_ambiguous_and_missing(cli_backend, tmp_path) -> None:
    runner = CliRunner()
    target = tmp_path / "out.jpg"
    cli_backend.enabled = False
    result = runner.invoke(app, ["contacts-photo", "Alice", "-o", str(target)])
    assert result.exit_code == 2 and "BLUEFERRY_CONTACT_PHOTOS" in result.output
    cli_backend.enabled = True

    cli_backend.matches = [("Alice", "+1"), ("Alicia", "+2")]
    result = runner.invoke(app, ["contacts-photo", "Ali", "-o", str(target)])
    assert result.exit_code == 2 and "2 contacts match" in result.output

    result = runner.invoke(app, ["contacts-photo", "+15559990000", "-o", str(target)])
    assert result.exit_code == 1
    assert not Path(target).exists()
