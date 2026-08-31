from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from blueferry import bluez_setup, config, pair_setup, quirks_report
from blueferry.quirks_report import MAX_REPORTS


@pytest.fixture(autouse=True)
def _pairing_diagnostics(monkeypatch):
    pair_setup._pending_teardown_traces.clear()
    monkeypatch.setattr(
        pair_setup,
        "_bluez_device_snapshot",
        lambda _path: {"device_present": False},
    )


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    state = tmp_path / "blueferry-state"
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "EVENTS_DB", state / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", state / "contacts.sqlite")
    return state


def test_pairing_outcome_separates_bond_from_later_setup_failure() -> None:
    attempt = {
        "phone": {"paired": True},
        "timeline": [{"event": "paired"}],
    }
    outcome = pair_setup._pairing_outcome(
        attempt, None, pair_setup.PairingError("advert failed"),
    )
    assert outcome["bonded"] is True
    assert outcome["setup_complete"] is False
    assert "pairing" not in outcome


def test_issue_instructions_ask_for_iphone_model_and_ios_version() -> None:
    text = quirks_report.issue_instructions("/tmp/quirks-test.json")
    assert "/tmp/quirks-test.json" in text
    assert "iPhone model" in text
    assert "iOS version" in text
    assert "blueferry pairing-issue" in quirks_report.cli_issue_hint()


def test_pairing_report_includes_the_running_build_sha(monkeypatch) -> None:
    monkeypatch.setattr(quirks_report, "running_build_sha", lambda: "a" * 64)
    monkeypatch.setattr(quirks_report, "installed_release", lambda: "0.6.3-1")

    attempt = quirks_report.start_attempt(interactive=True)

    assert attempt["blueferry_sha"] == "a" * 64
    assert attempt["blueferry_build"] == "0.6.3-1+sha." + "a" * 12


def test_issue_url_labels_a_pairing_issue_with_adapter_and_outcome() -> None:
    url = quirks_report.issue_url(
        {
            "controller": {
                "name": "hci0",
                "vendor": "Realtek",
                "product": "RTL8852CE",
            },
            "outcome": {"map": True, "pbap": True, "ancs": False},
        }
    )
    assert "labels=pairing-issue" in url
    title = parse_qs(urlparse(url).query)["title"][0]
    assert title == "Pairing issue: Realtek RTL8852CE — MAP/PBAP success, ANCS fail"
    assert "```json" in parse_qs(urlparse(url).query)["body"][0]
    assert '"ancs": false' in parse_qs(urlparse(url).query)["body"][0]
    assert quirks_report.issue_title(
        {"controller": {"name": "hci0"}, "outcome": {"bonded": True, "setup_complete": False}}
    ) == "Pairing issue: unknown adapter — bonded, setup failed"
    assert quirks_report.issue_title(
        {
            "controller": {
                "name": "hci0",
                "vendor": "MediaTek",
                "product": "Wireless_Device",
                "usb_id": "0e8d:7961",
            },
            "outcome": {"map": True, "pbap": True, "ancs": False},
        }
    ) == "Pairing issue: MediaTek MT7921 — MAP/PBAP success, ANCS fail"


def test_save_report_delta_compacts_repeated_bluez_snapshots(tmp_path) -> None:
    initial = {
        "device": {"paired": False, "trusted": False},
        "bearers": {"bredr": {"present": True}, "le": {"present": False}},
    }
    paired = {
        "device": {"paired": True, "trusted": False},
        "bearers": {
            "bredr": {"present": True, "connected": True},
            "le": {"present": True, "connected": False},
        },
    }
    disconnected = {
        "device": {"paired": True, "trusted": True},
        "bearers": {"bredr": {"present": True}, "le": {"present": False}},
    }

    path = quirks_report.save_report(
        {
            "bluez_trace": [
                {"t": 0.1, "phase": "device_loaded", "state": initial},
                {"t": 1.2, "phase": "paired", "state": paired},
                {"t": 2.3, "phase": "advert_ready", "state": paired},
                {"t": 3.4, "phase": "finished", "state": disconnected},
            ],
        },
        directory=tmp_path,
    )

    assert path is not None
    trace = json.loads(path.read_text())["bluez_trace"]
    assert trace[0]["state"] == initial
    assert trace[1]["changes"] == {
        "bearers": {
            "bredr": {"connected": True},
            "le": {"connected": False, "present": True},
        },
        "device": {"paired": True},
    }
    assert [entry["phase"] for entry in trace] == [
        "device_loaded", "paired", "finished",
    ]
    assert trace[2]["changes"] == {
        "bearers": {
            "bredr": {"connected": None},
            "le": {"connected": None, "present": False},
        },
        "device": {"trusted": True},
    }


def test_issue_url_compacts_full_snapshot_reports_before_embedding(monkeypatch) -> None:
    interfaces = {
        f"org.example.Interface{index}": index
        for index in range(18)
    }
    state = {
        "object_present": True,
        "device_present": True,
        "child_objects": 18,
        "root_interfaces": list(interfaces),
        "child_interfaces": interfaces,
        "device": {
            "paired": True,
            "trusted": True,
            "connected": True,
            "services_resolved": True,
            "ancs_uuid": False,
            "uuid_count": 14,
        },
        "bearers": {
            "bredr": {"present": True, "connected": True},
            "le": {"present": True, "connected": False},
        },
        "gatt": {
            "services": 0,
            "characteristics": 0,
            "ancs_service": False,
            "ancs_characteristics": [],
        },
        "battery_objects": 1,
    }
    payload = {
        "blueferry": "0.7.7",
        "blueferry_sha": "a" * 64,
        "controller": {
            "name": "hci0",
            "vendor": "Realtek",
            "product": "RTL8852CE",
            "supported_settings": list(interfaces),
            "current_settings": list(interfaces)[:10],
            "summary": "Realtek RTL8852CE (usb 0bda:c852, btusb)",
            "uuids": [
                *list(interfaces),
                "message-access-server",
                "phonebook-access-server",
            ],
        },
        "outcome": {"map": False, "pbap": False, "ancs": False},
        "timeline": [
            {"t": index * 1.5, "event": f"pairing_phase_{index}"}
            for index in range(30)
        ],
        "bluez_trace": [
            {"t": index * 5.0, "phase": f"phase_{index}", "state": state}
            for index in range(8)
        ],
    }
    monkeypatch.setattr(quirks_report, "MAX_ISSUE_URL_CHARS", 6_000)

    url = quirks_report.issue_url(payload)

    assert len(url) <= quirks_report.MAX_ISSUE_URL_CHARS
    body = parse_qs(urlparse(url).query)["body"][0]
    assert "too large to embed" not in body
    report_json = body.partition("```json\n")[2].rpartition("\n```")[0]
    embedded = json.loads(report_json)
    assert len(embedded["timeline"]) == 30
    assert len(embedded["bluez_trace"]) == 1
    issue_state = embedded["bluez_trace"][0]["state"]
    assert issue_state["device"] == state["device"]
    assert issue_state["bearers"] == state["bearers"]
    assert issue_state["gatt"] == state["gatt"]
    assert "root_interfaces" not in issue_state
    assert "child_interfaces" not in issue_state
    assert "summary" not in embedded["controller"]
    assert embedded["controller"]["messaging_uuids"] == [
        "message-access-server",
        "phonebook-access-server",
    ]


def test_issue_report_keeps_a_successful_ancs_setup(tmp_path) -> None:
    failed = quirks_report.save_report(
        {"outcome": {"setup_complete": True, "ancs": False}},
        directory=tmp_path,
    )
    assert quirks_report.issue_report(tmp_path) == failed
    succeeded = quirks_report.save_report(
        {"outcome": {"setup_complete": True, "ancs": True}},
        directory=tmp_path,
    )
    assert quirks_report.issue_report(tmp_path) == succeeded


def test_session_environment_reads_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert quirks_report.session_environment() == {
        "desktop": "KDE",
        "session_type": "wayland",
    }


def test_remember_daemon_status_splits_ancs_and_le_error() -> None:
    attempt = {"phone": {"le_bearer": {}}, "timeline": []}
    status = SimpleNamespace(
        extra={
            "ancs_subscribed": True,
            "ancs_authorized": False,
            "bredr": True,
            "le": False,
            "last_le_error": "org.bluez.Error.Failed",
            "last_le_error_message": "le-connection-abort-by-local",
        }
    )
    pair_setup._remember_daemon_status(attempt, status)
    assert attempt["daemon"]["ancs_subscribed"] is True
    assert attempt["daemon"]["ancs_authorized"] is False
    assert attempt["phone"]["le_bearer"]["last_error"] == "org.bluez.Error.Failed"
    assert (
        attempt["phone"]["le_bearer"]["last_error_message"]
        == "le-connection-abort-by-local"
    )
    assert [item["event"] for item in attempt["timeline"]] == [
        "ancs_subscribed",
        "le_connect_failed",
    ]
    assert attempt["timeline"][1]["error"] == "org.bluez.Error.Failed"
    assert attempt["timeline"][1]["message"] == "le-connection-abort-by-local"


def test_bluetooth_session_owners_lists_claimed_well_known_names(monkeypatch) -> None:
    claimed = {"org.blueman.Applet", "org.kde.kded6"}

    class Daemon:
        @staticmethod
        def NameHasOwner(name):
            return name in claimed

    class Bus:
        @staticmethod
        def get_object(service, path):
            assert service == "org.freedesktop.DBus"
            assert path == "/org/freedesktop/DBus"
            return object()

    monkeypatch.setattr(pair_setup, "get_session_bus", lambda: Bus())
    monkeypatch.setattr(pair_setup.dbus, "Interface", lambda *_args, **_kwargs: Daemon())
    assert pair_setup._bluetooth_session_owners() == [
        "org.blueman.Applet",
        "org.kde.kded6",
    ]


def test_wait_for_daemon_transports_records_split_ancs(monkeypatch) -> None:
    from blueferry.models import BackendStatus

    statuses = [
        BackendStatus.from_dict(
            {
                "map": True,
                "pbap": True,
                "ancs": False,
                "ancs_subscribed": True,
                "ancs_authorized": False,
                "bredr": True,
                "le": False,
                "last_le_error": "org.bluez.Error.Failed",
                "last_le_error_message": "le-connection-abort-by-local",
            }
        ),
        BackendStatus.from_dict(
            {
                "map": True,
                "pbap": True,
                "ancs": True,
                "ancs_subscribed": True,
                "ancs_authorized": True,
                "bredr": True,
                "le": True,
            }
        ),
    ]

    class FakeClient:
        @staticmethod
        def status():
            return statuses.pop(0)

    monkeypatch.setattr("blueferry.client.BackendClient", FakeClient)
    monkeypatch.setattr(pair_setup.time, "sleep", lambda _seconds: None)
    attempt = {"timeline": [], "phone": {"le_bearer": {}}}

    result = pair_setup._wait_for_daemon_transports(timeout=5, attempt=attempt)

    assert result.as_tuple() == (True, True, True)
    assert attempt["daemon"]["ancs_subscribed"] is True
    assert attempt["daemon"]["ancs_authorized"] is True
    assert "last_le_error" not in attempt["daemon"]
    assert attempt["phone"]["le_bearer"]["last_error"] == "org.bluez.Error.Failed"
    assert (
        attempt["phone"]["le_bearer"]["last_error_message"]
        == "le-connection-abort-by-local"
    )
    events = [item["event"] for item in attempt["timeline"]]
    assert events[:4] == [
        "waiting_for_transports",
        "ancs_subscribed",
        "le_connect_failed",
        "map_ready",
    ]
    assert "ancs_authorized" in events
    assert "ancs_ready" in events


def test_scrub_text_removes_mac_addresses_and_bluez_paths() -> None:
    raw = (
        "paired 02:00:AA:BB:CC:DD at "
        "/org/bluez/hci0/dev_02_00_AA_BB_CC_DD"
    )
    cleaned = quirks_report.scrub_text(raw)
    assert "02:00" not in cleaned
    assert "AA:BB:CC:DD" not in cleaned
    assert "dev_02_00" not in cleaned
    assert "xx:xx:xx:xx:xx:xx" in cleaned
    assert "dev_REDACTED" in cleaned


def test_scrub_text_replaces_home_and_xdg_prefixes(monkeypatch, tmp_path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    state = home / ".local" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    raw = (
        f"could not write {home / '.config/blueferry/local.env'}; "
        f"state={state / 'blueferry/events.sqlite'}"
    )
    cleaned = quirks_report.scrub_text(raw)
    assert "alice" not in cleaned
    assert str(home) not in cleaned
    assert "$XDG_CONFIG_HOME/blueferry/local.env" in cleaned
    assert "$XDG_STATE_HOME/blueferry/events.sqlite" in cleaned


def test_save_report_scrubs_home_paths_from_error_text(monkeypatch, tmp_path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = quirks_report.save_report(
        {"outcome": {"error": f"could not clear {home / '.config/blueferry/local.env'}"}},
        directory=tmp_path / "reports",
    )
    assert path is not None
    body = path.read_text()
    assert "alice" not in body
    assert "$XDG_CONFIG_HOME/blueferry/local.env" in body


def test_list_reports_ignores_files_removed_during_scan(tmp_path, monkeypatch) -> None:
    first = tmp_path / "quirks-a.json"
    second = tmp_path / "quirks-b.json"
    first.write_text("{}\n")
    second.write_text("{}\n")
    original = Path.lstat

    def flaky(self, *args, **kwargs):
        if self == first:
            raise FileNotFoundError(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", flaky)
    assert quirks_report.list_reports(tmp_path) == [second]


def test_public_device_omits_name_address_and_path() -> None:
    device = SimpleNamespace(
        mac="02:00:AA:BB:CC:DD",
        name="Alex's iPhone",
        icon="phone",
        likely_iphone=True,
        paired=True,
        trusted=True,
        connected=True,
        services_resolved=True,
        ancs_bonded=False,
        device_path="/org/bluez/hci0/dev_02_00_AA_BB_CC_DD",
    )
    public = quirks_report.public_device(device)
    encoded = json.dumps(public)
    assert "Alex" not in encoded
    assert "02:00" not in encoded
    assert "AA:BB" not in encoded
    assert "hci0" not in encoded
    assert "id" not in public
    assert public["likely_iphone"] is True
    assert public["ancs_uuid"] is False


def test_save_report_scrubs_payload_and_keeps_ten(tmp_path) -> None:
    directory = tmp_path / "blueferry"
    written = []
    for index in range(MAX_REPORTS + 3):
        written.append(
            quirks_report.save_report(
                {
                    "index": index,
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                },
                directory=directory,
            )
        )
    reports = sorted(directory.glob("quirks-*.json"))
    assert len(reports) == MAX_REPORTS
    bodies = [path.read_text() for path in reports]
    assert all("AA:BB:CC:DD:EE:FF" not in body for body in bodies)
    assert all("dev_AA_BB" not in body for body in bodies)
    assert all("xx:xx:xx:xx:xx:xx" in body for body in bodies)
    assert json.loads(written[-1].read_text())["index"] == MAX_REPORTS + 2


def _paired_device() -> pair_setup.PairedDevice:
    return pair_setup.PairedDevice(
        mac="02:00:00:00:00:01",
        name="Alex's iPhone",
        icon="phone",
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset({config.ANCS_SOLICIT_UUID}),
        services_resolved=True,
    )


def test_complete_pairing_writes_a_scrubbed_success_report(
    report_dir, monkeypatch,
) -> None:
    device = _paired_device()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "hardware_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
        },
    )
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(pair_setup, "_wait_for_classic_settled", lambda _path, **_kwargs: None)
    monkeypatch.setattr(
        pair_setup,
        "_bluetooth_session_owners",
        lambda: ["org.kde.BlueDevil.Client", "org.kde.kded6"],
    )

    def _ready_transports(*, attempt=None, **_kwargs):
        pair_setup._remember_daemon_status(
            attempt,
            SimpleNamespace(
                extra={
                    "ancs_subscribed": True,
                    "ancs_authorized": True,
                    "bredr": True,
                    "le": True,
                }
            ),
        )
        return pair_setup.PairingTransports(True, True, True)

    monkeypatch.setattr(pair_setup, "_wait_for_daemon_transports", _ready_transports)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)
    monkeypatch.setattr(pair_setup, "_adapter_identity", lambda adapter, *_args, **_kwargs: {"name": adapter})
    monkeypatch.setattr(
        pair_setup,
        "_le_bearer_snapshot",
        lambda _path: {
            "present": True, "paired": True, "bonded": True, "connected": True,
        },
    )
    pair_setup._pending_teardown_traces["hci0"] = {
        "reason": "forget_device",
        "remove_requested": True,
        "remove_result": "replied",
        "before_remove": {"device_present": True, "battery_objects": 1},
        "after_remove_reply": {"device_present": False, "battery_objects": 0},
    }

    result = pair_setup.complete_pairing(device.mac, _allow_headless=True)
    payload = Path(result.quirks_report).read_text()

    assert result.device.mac == device.mac
    assert result.ancs_ready is True
    assert "02:00:00:00:00:01" not in payload
    assert "Alex" not in payload
    assert "dev_02_00" not in payload
    parsed = json.loads(payload)
    assert parsed["outcome"]["bonded"] is True
    assert parsed["outcome"]["setup_complete"] is True
    assert "pairing" not in parsed["outcome"]
    assert parsed["outcome"]["map"] is True
    assert parsed["outcome"]["pbap"] is True
    assert parsed["outcome"]["ancs"] is True
    assert parsed["outcome"]["ancs_subscribed"] is True
    assert parsed["outcome"]["ancs_authorized"] is True
    assert "last_le_error" not in parsed["outcome"]
    assert parsed["session"] == {
        "desktop": "KDE",
        "session_type": "wayland",
        "bluetooth_owners": ["org.kde.BlueDevil.Client", "org.kde.kded6"],
    }
    assert "transports" not in parsed
    assert "id" not in parsed["phone"]
    assert parsed["phone"]["likely_iphone"] is True
    assert parsed["controller"]["name"] == "hci0"
    assert parsed["previous_teardown"] == {
        "reason": "forget_device",
        "remove_requested": True,
        "remove_result": "replied",
        "before_remove": {"device_present": True, "battery_objects": 1},
        "after_remove_reply": {"device_present": False, "battery_objects": 0},
        "before_new_pairing": {"device_present": False},
    }
    assert parsed["pairing_policy"] == {
        "ancs_capable": True,
        "ancs_enabled": True,
        "mode": "full",
        "pairing_strategy": "explicit-device-pair",
        "reason": "ancs-available",
        "solicitation_enabled": True,
        "user_forced": False,
    }
    assert parsed["pairing_options"] == {
        "compatibility_mode": False,
        "explicit_pairing": False,
    }
    assert "adapter" not in parsed
    assert "compatibility" not in parsed
    assert Path(result.quirks_report).is_relative_to(report_dir)
    events = [item["event"] for item in parsed["timeline"]]
    assert events[0] == "start"
    assert events[-1] == "finished"
    assert "prepare_classic_sent" in events
    assert "advert_ready" in events
    assert "advert_removed" in events
    assert "daemon_restarted" in events
    assert "ancs_subscribed" in events
    assert "ancs_authorized" in events
    assert "map_ready" in events
    times = [item["t"] for item in parsed["timeline"]]
    assert times[0] == 0
    assert times == sorted(times)
    assert "_t0" not in parsed
    assert parsed["duration_s"] >= times[-1]


def test_pairing_report_keeps_last_le_error_when_ancs_stays_down(
    report_dir, monkeypatch,
) -> None:
    device = _paired_device()
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "hardware_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
        },
    )
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(pair_setup, "_wait_for_classic_settled", lambda _path, **_kwargs: None)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "trust_device", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "write_local_env", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_restart_user_service", lambda: None)
    monkeypatch.setattr(pair_setup, "_adapter_identity", lambda adapter, *_args, **_kwargs: {"name": adapter})
    monkeypatch.setattr(
        pair_setup,
        "_le_bearer_snapshot",
        lambda _path: {
            "present": True, "paired": True, "bonded": False, "connected": False,
        },
    )

    def _ancs_missing(*, attempt=None, **_kwargs):
        pair_setup._remember_daemon_status(
            attempt,
            SimpleNamespace(
                extra={
                    "ancs_subscribed": False,
                    "ancs_authorized": False,
                    "bredr": True,
                    "le": False,
                    "last_le_error": "org.bluez.Error.Failed",
                    "last_le_error_message": "le-connection-abort-by-local",
                }
            ),
        )
        return pair_setup.PairingTransports(True, True, False)

    monkeypatch.setattr(pair_setup, "_wait_for_daemon_transports", _ancs_missing)

    result = pair_setup.complete_pairing(device.mac, _allow_headless=True)
    parsed = json.loads(Path(result.quirks_report).read_text())

    assert result.ancs_ready is False
    assert parsed["outcome"]["bonded"] is True
    assert parsed["outcome"]["setup_complete"] is True
    assert parsed["outcome"]["map"] is True
    assert parsed["outcome"]["ancs"] is False
    assert parsed["outcome"]["ancs_subscribed"] is False
    assert parsed["outcome"]["ancs_authorized"] is False
    assert parsed["outcome"]["last_le_error"] == "org.bluez.Error.Failed"
    assert parsed["outcome"]["last_le_error_message"] == "le-connection-abort-by-local"
    assert parsed["phone"]["le_bearer"]["bonded"] is False
    assert parsed["phone"]["le_bearer"]["last_error"] == "org.bluez.Error.Failed"
    assert parsed["phone"]["le_bearer"]["last_error_message"] == (
        "le-connection-abort-by-local"
    )
    assert "le_connect_failed" in [item["event"] for item in parsed["timeline"]]


def test_failed_pairing_still_writes_a_report(report_dir, monkeypatch) -> None:
    device = _paired_device()
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "hardware_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
            "low_energy": True,
            "advertising": True,
            "secure_conn": False,
            "current_settings": ["advertising", "br/edr", "le", "powered"],
            "supported_settings": [
                "advertising", "br/edr", "le", "powered", "secure-conn",
            ],
        },
    )
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: False)
    monkeypatch.setattr(
        pair_setup,
        "_le_bearer_snapshot",
        lambda _path: {
            "present": False, "paired": False, "bonded": False, "connected": False,
        },
    )
    monkeypatch.setattr(pair_setup, "_adapter_identity", lambda adapter, *_args, **_kwargs: {"name": adapter})

    with pytest.raises(pair_setup.PairingError) as raised:
        pair_setup.complete_pairing(device.mac, _allow_headless=True)

    path = Path(raised.value.report_path)
    payload = path.read_text()
    assert path.is_relative_to(report_dir)
    assert "02:00:00:00:00:01" not in payload
    assert "Alex" not in payload
    parsed = json.loads(payload)
    assert parsed["outcome"]["bonded"] is True
    assert parsed["outcome"]["setup_complete"] is False
    assert "pairing" not in parsed["outcome"]
    assert parsed["outcome"]["map"] is None
    assert parsed["outcome"]["ancs"] is None
    assert "error" in parsed["outcome"]
    assert parsed["pairing_options"] == {
        "compatibility_mode": False,
        "explicit_pairing": False,
    }
    events = [item["event"] for item in parsed["timeline"]]
    assert events[0] == "start"
    assert events[-1] == "failed"
    assert "prepare_classic_sent" in events
    assert "advert_removed" not in events
    assert "agent_released" not in events
    assert parsed["controller"]["notifications_supported"] is True
    assert parsed["controller"]["low_energy"] is True
    assert parsed["controller"]["advertising"] is True
    assert parsed["controller"]["secure_conn"] is False
    assert parsed["controller"]["current_settings"] == [
        "advertising", "br/edr", "le", "powered",
    ]
    assert "secure-conn" in parsed["controller"]["supported_settings"]
    assert parsed["controller"]["name"] == "hci0"
    assert "adapter" not in parsed
    assert "compatibility" not in parsed
    assert "device" not in parsed
    assert "desktop" in parsed["session"]
    assert "session_type" in parsed["session"]
    assert parsed["session"]["bluetooth_owners"] == []


def test_unexpected_pairing_exception_still_writes_a_report(
    report_dir, monkeypatch,
) -> None:
    device = _paired_device()
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "hardware_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
        },
    )
    monkeypatch.setattr(pair_setup, "_prefer_bredr", lambda _path: None)
    monkeypatch.setattr(pair_setup, "_activate_obex_mns", lambda: None)
    monkeypatch.setattr(pair_setup, "_wait_for_classic_settled", lambda _path, **_kwargs: None)
    monkeypatch.setattr(bluez_setup, "prepare_classic", lambda **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "register_advert", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bluez_setup, "unregister_advert", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "_adapter_identity", lambda adapter, *_args, **_kwargs: {"name": adapter})
    monkeypatch.setattr(
        pair_setup,
        "_le_bearer_snapshot",
        lambda _path: {
            "present": False, "paired": False, "bonded": False, "connected": False,
        },
    )
    monkeypatch.setattr(
        pair_setup,
        "trust_device",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("bluez properties vanished")),
    )

    with pytest.raises(RuntimeError, match="bluez properties vanished"):
        pair_setup.complete_pairing(device.mac, _allow_headless=True)

    report = quirks_report.latest_report(report_dir)
    assert report is not None
    parsed = json.loads(report.read_text())
    assert parsed["outcome"]["bonded"] is True
    assert parsed["outcome"]["setup_complete"] is False
    assert "bluez properties vanished" in str(parsed["outcome"]["error"])
    events = [item["event"] for item in parsed["timeline"]]
    assert "advert_removed" not in events
    assert "agent_released" not in events
