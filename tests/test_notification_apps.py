"""Muting iPhone apps: store, seen list, popup skip, D-Bus, CLI and presenter."""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import dbus
import dbus.service
import pytest

from blueferry import config
from blueferry.ancs.constants import MESSAGES_APP_ID, CategoryID
from blueferry.ancs.events import AncsEvent
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.dbus_service import MessagesService
from blueferry.errors import InvalidArgumentsError, NotReadyError
from blueferry.event_dispatcher import EventDispatcher
from blueferry.notification_apps import (
    SETTINGS_KEY,
    MutedAppsStore,
    NotificationApps,
    SeenApps,
    validate_app_id,
)
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE
from blueferry.settings_store import SettingsStore
from blueferry.sinks.libnotify import LibnotifySink
from blueferry.storage_security import (
    NO_STORAGE,
    PLAINTEXT_STORAGE,
    StorageSecurity,
    StorageUnavailableError,
)
from tests.test_calls_dbus import _call, _Sessions

_service_ids = itertools.count()


class _Wallet:
    def get_or_create(self, *, allow_prompt, cancellable=None, create=True):
        return b"K" * 32

    def delete(self, *, allow_prompt, cancellable=None):
        return True


@pytest.fixture
def storage(isolated_state):
    security = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    yield security
    security.close()


@pytest.fixture(autouse=True)
def no_env_rules(monkeypatch):
    monkeypatch.setattr(config, "ANCS_APP_ALLOWLIST", None)
    monkeypatch.setattr(config, "ANCS_APP_BLOCKLIST", frozenset())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        self.now += 1
        return self.now


def _apps(path, *, storage=None, policy="all", changes=None, muted_changes=None):
    return NotificationApps(
        MutedAppsStore(path, storage=storage),
        seen=SeenApps(clock=_Clock()),
        policy=lambda: policy,
        on_changed=(lambda: changes.append(True)) if changes is not None else None,
        on_muted_changed=(
            (lambda: muted_changes.append(True)) if muted_changes is not None else None
        ),
    )


# ---- store ------------------------------------------------------------------


def test_store_mutes_with_the_name_and_unmutes(tmp_path) -> None:
    store = MutedAppsStore(tmp_path / "settings.json")

    assert store.set_muted("com.example.Chat", True, "Chat") is True
    assert store.set_muted("com.example.Mail", True) is True
    assert store.entries() == {"com.example.Chat": "Chat", "com.example.Mail": ""}
    assert store.is_muted("com.example.Chat") is True
    assert store.set_muted("com.example.Chat", False) is False
    assert store.set_muted("com.example.Chat", False) is False
    assert store.entries() == {"com.example.Mail": ""}


def test_store_validates_ids_and_never_mutes_messages(tmp_path) -> None:
    store = MutedAppsStore(tmp_path / "settings.json")
    for bad in ("", "bad id", ".leading", "x" * 300, 42, MESSAGES_APP_ID, "com.apple.mobilesms"):
        with pytest.raises(ValueError):
            store.set_muted(bad, True)  # type: ignore[arg-type]
    assert validate_app_id("  com.example.App ") == "com.example.App"


def test_store_limit_and_bounded_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("blueferry.notification_apps.MAX_ANCS_MUTED_APPS", 2)
    store = MutedAppsStore(tmp_path / "settings.json")
    store.set_muted("com.example.One", True, "  One\nApp  " + "x" * 400)
    store.set_muted("com.example.Two", True)
    with pytest.raises(ValueError, match="at most 2"):
        store.set_muted("com.example.Three", True)
    # Unmuting and re-muting a listed app stay possible at the limit.
    assert store.set_muted("com.example.Two", True) is True
    name = store.entries()["com.example.One"]
    assert name.startswith("One App x") and len(name) == 256 and "\n" not in name


def test_store_ignores_corrupt_plaintext_records(tmp_path) -> None:
    path = tmp_path / "settings.json"
    SettingsStore(path).update(**{SETTINGS_KEY: [
        {"app_id": "com.example.Ok", "app_name": "Ok"},
        {"app_id": "bad id"}, "junk", {"app_id": MESSAGES_APP_ID},
        {"app_id": "com.example.Ok", "app_name": "Duplicate"},
        {"app_id": "com.example.Bare", "app_name": 5},
    ]})
    assert MutedAppsStore(path).entries() == {"com.example.Ok": "Ok", "com.example.Bare": ""}
    SettingsStore(path).update(**{SETTINGS_KEY: {"not": "a list"}})
    assert MutedAppsStore(path).entries() == {}


def test_store_is_sealed_under_the_storage_policy(storage) -> None:
    store = MutedAppsStore(config.SETTINGS_JSON, storage=storage)
    store.set_muted("com.example.Secret", True, "Secret Chat")

    raw = config.SETTINGS_JSON.read_text()
    assert "Secret" not in raw and "com.example" not in raw
    assert MutedAppsStore(config.SETTINGS_JSON, storage=storage).entries() == {
        "com.example.Secret": "Secret Chat",
    }


def test_locked_storage_mutes_nothing_and_never_writes(storage, isolated_state) -> None:
    MutedAppsStore(config.SETTINGS_JSON, storage=storage).set_muted("com.example.A", True)
    before = config.SETTINGS_JSON.read_text()
    locked = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
        initialize=False,
    )
    try:
        store = MutedAppsStore(config.SETTINGS_JSON, storage=locked)
        assert store.available is False
        assert store.entries() == {}
        assert store.is_muted("com.example.A") is False
        for muted in (True, False):
            with pytest.raises(StorageUnavailableError):
                store.set_muted("com.example.B", muted)
        assert store.rename("com.example.A", "New") is False
        assert config.SETTINGS_JSON.read_text() == before
    finally:
        locked.close()


def test_disabled_storage_offers_no_mutes(isolated_state) -> None:
    SettingsStore(config.SETTINGS_JSON).update(local_data=NO_STORAGE)
    disabled = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    try:
        assert disabled.status.policy == NO_STORAGE
        store = MutedAppsStore(config.SETTINGS_JSON, storage=disabled)
        assert store.available is False
        with pytest.raises(StorageUnavailableError):
            store.set_muted("com.example.A", True)
    finally:
        disabled.close()


def test_store_reads_settings_once_until_written_or_storage_changes(
    storage, isolated_state, monkeypatch,
) -> None:
    store = MutedAppsStore(config.SETTINGS_JSON, storage=storage)
    store.set_muted("com.example.A", True, "A")
    reads: list[bool] = []
    original = store._preference.read
    monkeypatch.setattr(
        store._preference, "read", lambda: reads.append(True) or original(),
    )
    for _ in range(3):  # one popup each, plus a GetStatus count
        assert store.is_muted("com.example.A") is True
    assert store.entries() == {"com.example.A": "A"}
    assert len(reads) == 1
    store.set_muted("com.example.B", True)
    assert store.is_muted("com.example.B") is True
    assert len(reads) == 2  # the write used the cached list, then reread
    storage.fail_closed("locked for the test")
    assert store.entries() == {}
    assert len(reads) == 3
    store.invalidate()
    store.entries()
    assert len(reads) == 4


def test_migrate_seals_legacy_plaintext(storage) -> None:
    SettingsStore(config.SETTINGS_JSON).update(**{SETTINGS_KEY: [
        {"app_id": "com.example.Legacy", "app_name": "Legacy"},
    ]})
    store = MutedAppsStore(config.SETTINGS_JSON, storage=storage)
    store.migrate()
    assert "Legacy" not in config.SETTINGS_JSON.read_text()
    assert store.entries() == {"com.example.Legacy": "Legacy"}


# ---- seen apps ----------------------------------------------------------------


def test_seen_apps_are_bounded_lru_and_learn_names_later() -> None:
    seen = SeenApps(maximum=2, clock=_Clock())
    assert seen.observe("com.example.A", "com.example.A") is True  # name unknown yet
    assert seen.get("com.example.A").app_name == ""
    assert seen.observe("com.example.A", "") is False
    assert seen.observe("com.example.A", "Alpha") is True
    assert seen.observe("com.example.A", "") is False
    assert seen.get("com.example.A").app_name == "Alpha"
    seen.observe("com.example.B", "Beta")
    seen.observe("com.example.A", "Alpha")  # A is now the most recent
    seen.observe("com.example.C", "Gamma")
    assert [app_id for app_id, _app in seen.items()] == ["com.example.A", "com.example.C"]


def test_observe_skips_messages_invalid_and_env_excluded(tmp_path, monkeypatch) -> None:
    changes: list[bool] = []
    apps = _apps(tmp_path / "s.json", changes=changes)
    monkeypatch.setattr(config, "ANCS_APP_BLOCKLIST", frozenset({"com.example.Blocked"}))
    for app_id in (MESSAGES_APP_ID, "bad id", None, "com.example.Blocked"):
        apps.observe(app_id, "Name")
    assert len(apps.seen) == 0 and changes == []
    apps.observe("com.example.Chat", "Chat")
    apps.observe("com.example.Chat", "Chat")
    assert changes == [True]


def test_dispatcher_records_only_non_messages_apps() -> None:
    seen = []
    dispatcher = EventDispatcher(
        None, defer_mark_read=lambda _handle: None,
        ancs_app_seen=lambda app_id, name: seen.append((app_id, name)),
        schedule=lambda *_args: 1, cancel=lambda _id: None,
    )
    for uid, app_id in enumerate((MESSAGES_APP_ID, "com.example.Chat")):
        dispatcher.ancs(AncsEvent(uid, app_id, "Chat", "t", "", "b"))
    # A replay is not a new notification.
    dispatcher.ancs(AncsEvent(1, "com.example.Chat", "Chat", "t", "", "b"))
    assert seen == [("com.example.Chat", "Chat")]


def test_muted_name_follows_a_later_name_without_writes_per_notification(tmp_path) -> None:
    path = tmp_path / "s.json"
    apps = _apps(path)
    apps.set_muted("com.example.Chat", True)  # muted before it was ever seen
    assert apps.store.entries() == {"com.example.Chat": ""}

    apps.observe("com.example.Chat", "Chat")
    assert apps.store.entries() == {"com.example.Chat": "Chat"}
    stamp = path.stat().st_mtime_ns
    for _ in range(5):
        apps.observe("com.example.Chat", "Chat")
        apps.observe("com.example.Other", "Other")
    assert path.stat().st_mtime_ns == stamp


# ---- payload ------------------------------------------------------------------


def test_payload_joins_seen_and_muted_sorted_by_name(tmp_path, monkeypatch) -> None:
    path = tmp_path / "s.json"
    MutedAppsStore(path).set_muted("com.example.Zed", True, "zed")
    MutedAppsStore(path).set_muted("com.example.Gone", True, "Gone")
    changes: list[bool] = []
    muted_changes: list[bool] = []
    apps = _apps(path, changes=changes, muted_changes=muted_changes)
    apps.observe("com.example.Alpha", "Alpha")
    apps.observe("com.example.Zed", "Zed")

    payload = apps.payload()
    assert [row["app_id"] for row in payload["apps"]] == [
        "com.example.Alpha", "com.example.Gone", "com.example.Zed",
    ]
    by_id = {row["app_id"]: row for row in payload["apps"]}
    assert by_id["com.example.Alpha"] == {
        "app_id": "com.example.Alpha", "app_name": "Alpha", "muted": False,
        "seen": True, "last_seen": 1001,
    }
    assert by_id["com.example.Gone"]["seen"] is False
    assert by_id["com.example.Gone"]["last_seen"] is None
    assert by_id["com.example.Zed"]["app_name"] == "Zed" and by_id["com.example.Zed"]["muted"]
    assert payload["muted_count"] == 2 and apps.muted_count() == 2
    assert payload["max_muted"] == 100 and payload["mute_available"] is True
    assert payload["all_notifications"] is True
    assert payload["env_allowlist"] is False and payload["env_blocklist"] is False

    changes.clear()
    assert apps.set_muted("com.example.Alpha", True) is True
    assert apps.set_muted("com.example.Alpha", True) is True
    assert changes == [True] and muted_changes == [True]
    assert apps.store.entries()["com.example.Alpha"] == "Alpha"

    monkeypatch.setattr(config, "ANCS_APP_ALLOWLIST", frozenset({"com.example.Alpha"}))
    narrowed = apps.payload()
    assert [row["app_id"] for row in narrowed["apps"]] == ["com.example.Alpha"]
    assert narrowed["env_allowlist"] is True and apps.muted_count() == 1
    with pytest.raises(ValueError, match="ALLOWLIST"):
        apps.set_muted("com.example.Zed", False)
    # An env-excluded app is never treated as muted (it is never fetched).
    assert apps.is_muted("com.example.Zed") is False


def test_payload_reports_policy_and_blocklist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "ANCS_APP_BLOCKLIST", frozenset({"com.example.X"}))
    payload = _apps(tmp_path / "s.json", policy="messages").payload()
    assert payload["all_notifications"] is False and payload["env_blocklist"] is True


# ---- desktop popups -------------------------------------------------------------


class _Notifications:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def Notify(self, *args):
        self.calls.append(args)
        return len(self.calls)


def _popup_sink(muted: set[str]) -> LibnotifySink:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _Notifications()
    sink._app_muted = lambda app_id: app_id in muted
    return sink


def _event(app_id, category=CategoryID.Other):
    return AncsEvent(
        notification_id=7, app_id=app_id, app_name="App", title="Anna", subtitle="",
        body="Hello", category=category, seen_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_muted_app_shows_no_popup_and_the_check_is_live(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    muted = {"com.example.Muted"}
    sink = _popup_sink(muted)

    sink.handle_ancs(_event("com.example.Muted"))
    assert sink._notif.calls == []
    sink.handle_ancs(_event("com.example.Other"))
    assert len(sink._notif.calls) == 1

    muted.clear()  # unmuted from a client: no restart needed
    sink.handle_ancs(_event("com.example.Muted"))
    assert len(sink._notif.calls) == 2


def test_failing_mute_check_still_shows_the_popup(monkeypatch) -> None:
    sink = _popup_sink(set())
    sink._app_muted = lambda _app_id: 1 / 0
    sink.handle_ancs(_event("com.example.Other"))
    assert len(sink._notif.calls) == 1


def test_muted_phone_app_keeps_the_missed_call_mark() -> None:
    sink = _popup_sink({"com.apple.mobilephone"})
    sink.handle_ancs(_event("com.apple.mobilephone", CategoryID.MissedCall))
    assert sink._notif.calls == []
    assert len(sink._ancs_missed_call_marks()) == 1


def test_dispatcher_hands_the_live_check_to_the_popup_sink() -> None:
    built = {}
    dispatcher = EventDispatcher(
        None, defer_mark_read=lambda _handle: None,
        ancs_app_muted=lambda app_id: app_id == "com.example.Muted",
        notification_sink_factory=lambda **kwargs: built.update(kwargs) or SimpleNamespace(
            name="libnotify",
        ),
        session_bus=SimpleNamespace(add_signal_receiver=lambda *_a, **_k: None),
        otp_autocopy=lambda: False,
        schedule=lambda *_args: 1, cancel=lambda _id: None,
    )
    dispatcher._setup_complete = True
    assert dispatcher._ensure_libnotify_sink() is True
    assert built["app_muted"]("com.example.Muted") is True


# ---- backend operations and the daemon -------------------------------------------


def test_operations_map_errors(tmp_path, storage) -> None:
    apps = _apps(config.SETTINGS_JSON, storage=storage)
    operations = BackendOperations(_Sessions(), BackendDependencies(notification_apps=apps))
    assert operations.set_notification_app_muted("com.example.A", True) is True
    assert operations.status()["notification_muted_app_count"] == 1
    with pytest.raises(InvalidArgumentsError):
        operations.set_notification_app_muted("bad id", True)
    with pytest.raises(NotReadyError):
        BackendOperations(_Sessions(), BackendDependencies()).list_notification_apps()
    assert "notification_muted_app_count" not in BackendOperations(
        _Sessions(), BackendDependencies(),
    ).status()

    locked_apps = _apps(config.SETTINGS_JSON, storage=SimpleNamespace(
        status=SimpleNamespace(can_read=False, can_write=False, policy="encrypted"),
    ))
    locked = BackendOperations(_Sessions(), BackendDependencies(notification_apps=locked_apps))
    with pytest.raises(NotReadyError, match="locked"):
        locked.set_notification_app_muted("com.example.A", False)
    assert locked.list_notification_apps()["mute_available"] is False


def test_changing_the_storage_policy_clears_mutes(storage) -> None:
    apps = _apps(config.SETTINGS_JSON, storage=storage)
    apps.set_muted("com.example.A", True)
    operations = BackendOperations(_Sessions(), BackendDependencies(
        storage=storage, notification_apps=apps,
    ))
    # Sealed under the old policy, the list could not be read afterwards.
    assert operations._prepare_storage_policy(PLAINTEXT_STORAGE) == PLAINTEXT_STORAGE
    assert SettingsStore(config.SETTINGS_JSON).read().get(SETTINGS_KEY) is None


def test_daemon_wires_seen_apps_mutes_and_status(make_daemon, monkeypatch, storage) -> None:
    daemon = make_daemon()
    # The daemon's own keyring is not available here; keep its wiring.
    daemon.notification_apps.store = MutedAppsStore(config.SETTINGS_JSON, storage=storage)
    signals = []
    daemon._dbus_service = SimpleNamespace(
        emit_notification_apps_changed=lambda: signals.append("apps"),
        emit_status=lambda: signals.append("status"),
    )
    monkeypatch.setattr(daemon.notification_policy, "_value", "all")
    daemon.events.ancs(AncsEvent(3, "com.example.Chat", "Chat", "t", "", "b"))
    assert signals == ["apps"]
    assert daemon.events.ancs_app_muted("com.example.Chat") is False

    payload = daemon.notification_apps.payload()
    assert [row["app_id"] for row in payload["apps"]] == ["com.example.Chat"]
    assert payload["all_notifications"] is True
    assert daemon.notification_apps.set_muted("com.example.Chat", True) is True
    assert daemon.events.ancs_app_muted("com.example.Chat") is True
    assert signals[-2:] == ["apps", "status"]
    assert daemon.notification_apps.muted_count() == 1

    invalidated = []
    monkeypatch.setattr(
        daemon.notification_apps.store, "invalidate", lambda: invalidated.append(True),
    )
    signals.clear()
    daemon._on_storage_changed()
    assert invalidated and signals[0] == "apps"


# ---- D-Bus round trip ----------------------------------------------------------


@pytest.mark.private_dbus
def test_notification_apps_round_trip_over_the_bus(tmp_path) -> None:
    apps = _apps(tmp_path / "settings.json")
    apps.observe("com.example.Chat", "Chat")
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Apps{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False}, notification_apps=apps,
        ),
    )
    try:
        muted = _call(name, MESSAGES_IFACE, "SetNotificationAppMuted", "com.example.Chat", True)
        listed = _call(name, MESSAGES_IFACE, "ListNotificationApps")
        invalid = _call(name, MESSAGES_IFACE, "SetNotificationAppMuted", MESSAGES_APP_ID, True)
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert muted["value"] is not None and bool(muted["value"]) is True
    payload = json.loads(listed["value"])
    assert payload["apps"][0]["muted"] is True and payload["apps"][0]["app_name"] == "Chat"
    assert invalid["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.InvalidArgs"
