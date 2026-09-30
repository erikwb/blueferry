"""Call history through the daemon, backend operations, sinks, and D-Bus."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from blueferry import config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.call_history import (
    INCOMING,
    MISSED,
    OUTGOING,
    CallRecord,
    MissedCallNotice,
)
from blueferry.call_history_repository import CallHistoryRepository
from blueferry.dbus_service import MessagesService
from blueferry.errors import NotReadyError, OperationFailedError
from blueferry.event_dispatcher import EventDispatcher
from blueferry.settings_store import SettingsStore
from blueferry.sinks.libnotify import LibnotifySink
from blueferry.storage_preparation import prepare_storage
from blueferry.storage_security import StorageSecurity

# A fixed instant keeps these tests deterministic. Retention is widened so
# repository calls that use the real clock never age these records out.
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def long_retention(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 3650)


def _call(direction, minutes_ago, number="15551230001", name=None) -> CallRecord:
    moment = NOW - timedelta(minutes=minutes_ago)
    return CallRecord(
        direction=direction, occurred_at=moment,
        raw_time=moment.strftime("%Y%m%dT%H%M%SZ"),
        address=f"+{number}", phone=number, name=name,
    )


class _Wallet:
    def get_or_create(self, *, allow_prompt, cancellable=None):
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


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error):
        pass


class _Contacts:
    def __init__(self, names):
        self.names = names

    def resolve(self, raw):
        digits = "".join(character for character in str(raw or "") if character.isdigit())
        return self.names.get(digits)


class _History:
    def __init__(self, records=()):
        self._records = list(records)
        self.discarded = 0
        self.sync_calls = []

    def records(self):
        return list(self._records)

    def sync(self, success, failure):
        self.sync_calls.append((success, failure))

    def discard_cache(self):
        self.discarded += 1
        self._records = []


# ---- backend operations ------------------------------------------------------

def test_disabled_feature_is_inert_and_explains_itself(storage) -> None:
    operations = BackendOperations(_Sessions(), BackendDependencies(storage=storage))

    with pytest.raises(NotReadyError, match="BLUEFERRY_CALL_HISTORY_ENABLED"):
        operations.list_call_history(10)
    with pytest.raises(NotReadyError, match="disabled"):
        operations.sync_call_history(lambda _count: None, lambda _error: None)
    assert not config.CALLS_DB.exists()


def test_list_applies_contact_names_bounds_and_newest_first_order(storage) -> None:
    history = _History([
        _call(MISSED, 1, "15551230001", name="Card Name"),
        _call(OUTGOING, 2, "15551230002"),
        _call(INCOMING, 3, "15551230003", name="Only On Card"),
        _call(INCOMING, 4, "0"),
    ])
    operations = BackendOperations(_Sessions(), BackendDependencies(
        storage=storage,
        contacts=_Contacts({"15551230001": "Anna Muster", "15551230002": "Ben"}),
        call_history=history,
    ))

    listed = operations.list_call_history(0)  # clamped to at least one
    full = operations.list_call_history(10_000_000)

    assert len(listed) == 1
    assert [entry["direction"] for entry in full] == [MISSED, OUTGOING, INCOMING, INCOMING]
    assert full[0] == {
        "direction": MISSED,
        "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
        "address": "+15551230001",
        "name": "Anna Muster",
        "contact_name": "Anna Muster",
    }
    assert (full[2]["name"], full[2]["contact_name"]) == ("Only On Card", None)
    assert (full[3]["name"], full[3]["contact_name"]) == (None, None)


def test_list_refuses_while_storage_is_locked(isolated_state) -> None:
    locked = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
        initialize=False,
    )
    operations = BackendOperations(_Sessions(), BackendDependencies(
        storage=locked, call_history=_History([_call(MISSED, 1)]),
    ))

    with pytest.raises(NotReadyError):
        operations.list_call_history(10)


def test_manual_sync_requires_pbap_and_wraps_failures(storage) -> None:
    history = _History()
    no_pbap = SimpleNamespace(map=object(), pbap=None, map_path="/m", report_error=print)
    with pytest.raises(NotReadyError, match="PBAP"):
        BackendOperations(no_pbap, BackendDependencies(
            storage=storage, call_history=history,
        )).sync_call_history(lambda _count: None, lambda _error: None)

    failures = []
    BackendOperations(_Sessions(), BackendDependencies(
        storage=storage, call_history=history,
    )).sync_call_history(lambda _count: None, failures.append)
    _success, failure = history.sync_calls[-1]
    failure(RuntimeError("transfer failed"))

    assert isinstance(failures[0], OperationFailedError)
    assert failures[0].dbus_suffix == "CallHistorySyncFailed"


def test_clear_history_erases_call_history_and_rearms_seeding(storage) -> None:
    CallHistoryRepository(storage).replace([_call(MISSED, 1)])
    history = _History([_call(MISSED, 1)])
    operations = BackendOperations(_Sessions(), BackendDependencies(
        storage=storage, call_history=history,
    ))

    operations.clear_history(True)

    assert history.discarded == 1
    assert CallHistoryRepository(storage).load() == []
    assert CallHistoryRepository(storage).replace([_call(MISSED, 0)]).seeded


def test_clear_history_erases_leftovers_even_when_disabled(storage) -> None:
    CallHistoryRepository(storage).replace([_call(MISSED, 1)])

    BackendOperations(_Sessions(), BackendDependencies(storage=storage)).clear_history(True)

    assert CallHistoryRepository(storage).load() == []


# ---- storage preparation -------------------------------------------------------

def test_preparation_prunes_and_loads_when_enabled(storage, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", True)
    CallHistoryRepository(storage).replace([_call(OUTGOING, 5)])

    prepared = prepare_storage(storage)

    assert prepared.call_history == [_call(OUTGOING, 5)]


def test_preparation_erases_call_history_once_the_feature_is_off(storage, monkeypatch) -> None:
    CallHistoryRepository(storage).replace([_call(OUTGOING, 5)])
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", False)

    prepared = prepare_storage(storage)

    assert prepared.call_history == []
    assert CallHistoryRepository(storage).load() == []


# ---- daemon wiring -----------------------------------------------------------

def test_daemon_without_opt_in_has_no_call_history(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", False)
    daemon = make_daemon()
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})

    assert daemon.call_history is None
    status = daemon._status()
    assert status["call_history_enabled"] is False
    assert status["missed_call_notifications"] is False
    daemon._post_available_sessions_setup()
    assert not config.CALLS_DB.exists()


def test_daemon_opt_in_resolves_callers_and_notifies(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", True)
    monkeypatch.setattr(config, "MISSED_CALL_NOTIFICATIONS", True)
    daemon = make_daemon()
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})
    monkeypatch.setattr(
        daemon.contacts, "resolve",
        lambda raw: "Anna Muster" if raw == "15551230001" else None,
    )
    delivered = []
    monkeypatch.setattr(daemon.events, "missed_calls", delivered.append)

    assert daemon.call_history is not None
    assert daemon._status()["call_history_enabled"] is True
    daemon._missed_calls([
        _call(MISSED, 1, "15551230001"),
        _call(MISSED, 2, "15551230002", name="Card Name"),
        _call(MISSED, 3, "0"),
    ])

    assert [(notice.caller, notice.known_contact) for notice in delivered[0]] == [
        ("Anna Muster", True), ("Card Name", False), ("+0", False),
    ]


def test_daemon_missed_call_sub_flag_silences_popups(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", True)
    monkeypatch.setattr(config, "MISSED_CALL_NOTIFICATIONS", False)
    daemon = make_daemon()
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})
    delivered = []
    monkeypatch.setattr(daemon.events, "missed_calls", delivered.append)

    daemon._missed_calls([_call(MISSED, 1)])

    assert delivered == []
    assert daemon._status()["missed_call_notifications"] is False


# ---- desktop sink ------------------------------------------------------------

class _FakeNotifications:
    def __init__(self):
        self.calls = []

    def Notify(self, *args):
        self.calls.append(args)
        return len(self.calls)


def _sink(policy="messages", contacts_only=False) -> LibnotifySink:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: policy
    sink._contacts_only_notifications = lambda: contacts_only
    sink._notif = _FakeNotifications()
    return sink


def _notice(caller="Anna <b>", known=True, minutes_ago=1) -> MissedCallNotice:
    return MissedCallNotice(
        caller=caller, known_contact=known,
        occurred_at=NOW - timedelta(minutes=minutes_ago),
    )


def test_missed_call_popup_escapes_caller(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    sink = _sink()

    sink.handle_missed_calls([_notice()])

    [call] = sink._notif.calls
    assert "Anna &lt;b&gt;" in call[3]
    assert call[4].startswith("Missed call")
    assert call[5] == [], "no actions: nothing to open for a call"


def test_hidden_content_omits_caller_and_time(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", False)
    sink = _sink()

    sink.handle_missed_calls([_notice(caller="+41 79 555 01 23"), _notice(caller=None)])

    for call in sink._notif.calls:
        assert "555" not in call[3] + call[4]
        assert "Anna" not in call[3] + call[4]
    assert len(sink._notif.calls) == 2


def test_burst_collapses_into_one_summary(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", False)
    sink = _sink()

    sink.handle_missed_calls([_notice(minutes_ago=index) for index in range(10)])

    [call] = sink._notif.calls
    assert "10 missed calls" in call[3]
    assert "Anna" not in call[4]


@pytest.mark.parametrize(("policy", "contacts_only", "expected"), [
    ("none", False, 0), ("messages", True, 1), ("all", False, 2),
])
def test_popup_policy_is_respected(monkeypatch, policy, contacts_only, expected) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    sink = _sink(policy, contacts_only)

    sink.handle_missed_calls([_notice(known=True), _notice(caller="+1555", known=False)])

    assert len(sink._notif.calls) == expected


def test_dispatcher_fans_missed_calls_only_to_capable_sinks() -> None:
    received = []
    dispatcher = EventDispatcher(SimpleNamespace(), defer_mark_read=lambda _path: None)
    dispatcher.dbus_service = SimpleNamespace(
        emit_history_changed=lambda: received.append("history"),
    )
    dispatcher.sinks = [
        SimpleNamespace(name="sqlite", handle=lambda _event: received.append("stored")),
        SimpleNamespace(name="libnotify", handle_missed_calls=received.append),
    ]

    dispatcher.missed_calls([_notice()])

    assert received == [[_notice()]]


# ---- D-Bus -------------------------------------------------------------------

def test_call_history_signal_carries_no_payload() -> None:
    signal = MessagesService.CallHistoryChanged
    assert signal._dbus_signature == ""
    method = MessagesService.ListCallHistory
    assert (method._dbus_in_signature, method._dbus_out_signature) == ("u", "s")


def test_daemon_request_hook_is_a_no_op_when_disabled(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", False)
    daemon = make_daemon()

    daemon.request_call_history_sync("call ended")  # must not raise

    monkeypatch.setattr(config, "CALL_HISTORY_ENABLED", True)
    enabled = make_daemon()
    requested = []
    monkeypatch.setattr(enabled.call_history, "request_sync", requested.append)
    enabled.request_call_history_sync("call ended")
    assert requested == ["call ended"]


def test_listing_resolves_double_zero_numbers_through_the_plus_form(storage) -> None:
    record = CallRecord(
        direction=MISSED, occurred_at=NOW, raw_time="20260928T120000Z",
        address="0041 79 555 01 23", phone="0041795550123", name=None,
    )
    operations = BackendOperations(_Sessions(), BackendDependencies(
        storage=storage,
        contacts=SimpleNamespace(
            resolve=lambda raw: "Anna" if raw == "+41795550123" else None,
        ),
        call_history=_History([record]),
    ))

    [entry] = operations.list_call_history(5)

    assert entry["contact_name"] == "Anna"
