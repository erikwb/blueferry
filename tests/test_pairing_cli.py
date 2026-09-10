from types import SimpleNamespace

import pytest

from blueferry import pairing_cli
from blueferry.bluetooth_devices import PairedDevice
from blueferry.pairing_cli import _print_ancs_repair_hint, _print_iphone_steps
from blueferry.setup_client import DISCOVERY_SECONDS, BluetoothCompatibility
from blueferry.setup_verification import CONTACTS


def test_verified_cli_setup_omits_the_iphone_section(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(),
        notifications_supported=True,
        ancs_enabled=True,
        ancs_ready=True,
    )

    assert capsys.readouterr().out == ""


def test_cli_ancs_repair_hint_recommends_bluez_restart(capsys) -> None:
    _print_ancs_repair_hint()

    output = capsys.readouterr().out
    assert "If ANCS remains unavailable after setup" in output
    assert "sudo systemctl restart bluetooth.service" in output
    assert "wait for BlueFerry to reconnect" in output
    assert "briefly disconnects all Bluetooth devices" in output


def test_cli_limited_vendor_treats_missing_ancs_as_expected(capsys) -> None:
    _print_ancs_repair_hint(limited=True, vendor="Realtek")

    output = capsys.readouterr().out
    assert "This Realtek adapter does not support iPhone system notifications" in output
    assert "sudo systemctl restart" not in output


def test_cli_setup_always_prints_required_bluetooth_toggles(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(CONTACTS,),
        notifications_supported=True,
        ancs_enabled=True,
        ancs_ready=True,
    )

    output = capsys.readouterr().out
    assert "3. Allow Notification Access when prompted" in output
    assert "4. Toggle on Show Message Notifications and Sync Contacts" in output
    assert "back out and tap the (i) again" in output
    assert "Sync Contacts" in output
    assert "Show Message Notifications" in output


def test_cli_explains_group_threads_need_notification_access(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=("notification-access",),
        notifications_supported=True,
        ancs_enabled=True,
        ancs_ready=False,
    )

    assert (
        "Without System Notification access, group texts appear as individual "
        "conversations with their sender."
    ) in capsys.readouterr().out


def test_cli_compatibility_mode_does_not_claim_the_controller_lacks_ancs(
    capsys,
) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(CONTACTS,),
        notifications_supported=True,
        ancs_enabled=False,
        ancs_ready=False,
    )

    output = capsys.readouterr().out
    assert "Without System Notification access" in output
    assert "controller supports Messages and Contacts, but not" not in output


def test_cli_just_works_pairing_still_requires_confirmation(monkeypatch) -> None:
    prompts = []
    monkeypatch.setattr(
        pairing_cli.typer,
        "confirm",
        lambda prompt, **kwargs: prompts.append((prompt, kwargs)) or False,
    )

    assert pairing_cli._confirm_pairing(None) is False
    assert prompts == [
        ("Approve this Bluetooth pairing request?", {"default": False})
    ]


def test_cli_verification_distinguishes_compatibility_mode_from_missing_ancs() -> None:
    assert pairing_cli._verification_detail(
        ancs_ready=False,
        compatibility_mode=True,
    ) == "messages and contacts; ANCS was disabled by compatibility mode"
    assert pairing_cli._verification_detail(
        ancs_ready=False,
        compatibility_mode=False,
    ) == "messages and contacts; this controller has no ANCS support"


def test_cli_requires_phone_side_forget_before_clearing_saved_target(
    monkeypatch,
    capsys,
) -> None:
    prompts = []
    forgotten = []

    class Setup:
        @staticmethod
        def configuration():
            return SimpleNamespace(saved=True, mac="02:00:00:00:00:01", adapter="hci1")

        @staticmethod
        def compatibility():
            return SimpleNamespace(
                adapter="hci1", pairing_ready=True, adapters=(), explicit_pairing_default=False,
            )

        @staticmethod
        def forget(mac, *, adapter=None):
            forgotten.append((mac, adapter))
            raise pairing_cli.PairingError("stop after forget")

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(
        pairing_cli.typer,
        "confirm",
        lambda prompt, **kwargs: prompts.append((prompt, kwargs)) or True,
    )

    assert pairing_cli.run_wizard(verify_after=False) == 1
    assert forgotten == [("02:00:00:00:00:01", "hci1")]
    assert prompts == [
        (
            "Forget BlueFerry's configured target and start fresh?",
            {"default": False},
        )
    ]
    output = capsys.readouterr().out
    assert "Before answering Yes, forget this PC on the iPhone too" in output
    assert "Forget This Device" in output


def test_cli_labels_adapter_choices_and_checks_an_explicit_incompatible_choice(monkeypatch, capsys):
    compatibility = BluetoothCompatibility.from_dict({
        "adapter": "hci1", "pairing_ready": True,
        "adapters": [
            {"name": "hci0", "label": "Built-in (hci0)", "available": True,
             "pairing_ready": False},
            {"name": "hci1", "label": "Dongle (hci1)", "available": False,
             "pairing_ready": True},
        ],
    })
    selected = []

    class Setup:
        def configuration(self):
            return SimpleNamespace(saved=True)

        def compatibility(self, adapter=None):
            selected.append(adapter)
            if adapter is None:
                return compatibility
            return BluetoothCompatibility.from_dict({
                "adapter": adapter, "pairing_ready": False,
                "issue": "Incompatible Bluetooth adapter: missing Bluetooth LE",
            })

        def forget(self, *_args, **_kwargs):
            pytest.fail("removed the saved phone for an incompatible choice")

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(pairing_cli.typer, "prompt", lambda *_a, **_kw: "1")
    assert pairing_cli.run_wizard(verify_after=False) == 1
    assert selected == [None, "hci0"]
    output = capsys.readouterr().out
    assert "[1] Built-in (hci0) (incompatible)" in output
    assert "[2] Dongle (hci1) (selected) (unverified)" in output
    assert "Incompatible Bluetooth adapter: missing Bluetooth LE" in output


@pytest.mark.parametrize("saved", [False, True])
def test_cli_incompatible_adapter_stops_before_scanning_or_forgetting(monkeypatch, capsys, saved):
    message = "Incompatible Bluetooth adapter: missing Bluetooth LE"

    class Setup:
        @staticmethod
        def configuration():
            return SimpleNamespace(saved=saved, mac="02:00:00:00:00:01", adapter="hci0")

        @staticmethod
        def compatibility():
            return SimpleNamespace(adapter="hci0", adapters=(), pairing_ready=False, issue=message)

        @staticmethod
        def devices(**_kwargs):
            pytest.fail("scanned with an incompatible adapter")

        @staticmethod
        def forget(*_args, **_kwargs):
            pytest.fail("removed a bond with an incompatible adapter")

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(
        pairing_cli.typer, "confirm",
        lambda *_args, **_kwargs: pytest.fail("prompted for incompatible pairing"),
    )
    assert pairing_cli.run_wizard(verify_after=False, compatibility_mode=True) == 1
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(
    ("hardware_supported", "notifications_supported", "issue", "expected_mode"),
    [
        (True, True, "", False),
        (False, False, "btmgmt timed out", True),
    ],
)
@pytest.mark.parametrize("default,override,expected_explicit", [
    (False, None, False), (True, None, True), (True, False, False), (False, True, True),
])
def test_cli_wizard_supplies_its_own_pairing_agent_ui(
    monkeypatch,
    capsys,
    hardware_supported,
    notifications_supported,
    issue,
    expected_mode,
    default,
    override,
    expected_explicit,
) -> None:
    prompts = []
    observed = []
    device = PairedDevice(
        mac="02:00:00:00:00:01",
        name="iPhone",
        icon="phone",
        trusted=False,
        connected=False,
        paired=False,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset(),
    )
    compatibility = SimpleNamespace(
        adapter="hci0",
        hardware_supported=hardware_supported,
        pairing_ready=True,
        explicit_pairing_default=default,
        issue=issue,
        notifications_supported=notifications_supported,
        bearer_api_active=True,
        adapters=(),
    )

    class Setup:
        @staticmethod
        def compatibility():
            return compatibility

        @staticmethod
        def devices(*, scan_seconds, adapter=None):
            assert scan_seconds == DISCOVERY_SECONDS
            return [device]

        @staticmethod
        def configuration():
            return SimpleNamespace(saved=False, mac="")

        @staticmethod
        def complete(
            mac,
            *,
            confirmation,
            display,
            adapter=None,
            compatibility_mode=False,
            explicit_pairing=False,
        ):
            assert compatibility_mode is expected_mode
            assert explicit_pairing is expected_explicit
            observed.append((mac, adapter, confirmation(12345)))
            display(12345)
            return SimpleNamespace(device=device, ancs_ready=True)

    class Backend:
        @staticmethod
        def status():
            return SimpleNamespace(verified_iphone_setup=())

    def confirm(prompt, **kwargs):
        prompts.append((prompt, kwargs))
        return True

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(pairing_cli, "BackendClient", Backend)
    monkeypatch.setattr(pairing_cli.typer, "confirm", confirm)
    monkeypatch.setattr(pairing_cli, "_print_iphone_steps", lambda *_args, **_kwargs: None)

    assert pairing_cli.run_wizard(verify_after=False, explicit_pairing=override) == 0
    assert observed == [(device.mac, "hci0", True)]
    assert prompts == [
        ("\nUse this device?", {"default": True}),
        ("Do both devices show Bluetooth code 012345?", {"default": False}),
    ]
    output = capsys.readouterr().out
    assert "Bluetooth pairing code: 012345" in output
    assert "pairing-issue" not in output
    if not hardware_supported:
        assert "btmgmt timed out" in output
        assert "Pairing will still be attempted in compatibility mode" in output
        assert "Automatic compatibility mode" in output


def test_cli_wizard_preserves_report_for_unexpected_pairing_failure(
    monkeypatch,
    capsys,
) -> None:
    device = PairedDevice(
        mac="02:00:00:00:00:01",
        name="iPhone",
        icon="phone",
        trusted=False,
        connected=False,
        paired=False,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset(),
    )

    class Setup:
        @staticmethod
        def configuration():
            return SimpleNamespace(saved=False, mac="")

        @staticmethod
        def compatibility():
            return SimpleNamespace(
                adapter="hci0",
                hardware_supported=True,
                pairing_ready=True,
                explicit_pairing_default=False,
                issue="",
                notifications_supported=True,
                bearer_api_active=True,
                adapters=(),
            )

        @staticmethod
        def devices(*, scan_seconds, adapter=None):
            return [device]

        @staticmethod
        def complete(*_args, **_kwargs):
            raise RuntimeError("unexpected pairing failure")

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(
        pairing_cli,
        "latest_report",
        lambda: "/tmp/quirks-unexpected.json",
    )
    monkeypatch.setattr(
        pairing_cli.typer,
        "confirm",
        lambda *_args, **_kwargs: True,
    )

    assert pairing_cli.run_wizard(verify_after=False) == 1
    output = capsys.readouterr().out
    assert "unexpected pairing failure" in output
    assert "blueferry pairing-issue" in output


def test_cli_wizard_points_at_pairing_issue_when_ancs_stays_down(
    monkeypatch, capsys,
) -> None:
    device = PairedDevice(
        mac="02:00:00:00:00:01",
        name="iPhone",
        icon="phone",
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset(),
    )
    compatibility = SimpleNamespace(
        adapter="hci0",
        hardware_supported=True,
        pairing_ready=True,
        explicit_pairing_default=False,
        issue="",
        notifications_supported=True,
        bearer_api_active=True,
        adapters=(),
    )

    class Setup:
        @staticmethod
        def compatibility():
            return compatibility

        @staticmethod
        def devices(*, scan_seconds, adapter=None):
            return [device]

        @staticmethod
        def configuration():
            return SimpleNamespace(saved=False, mac="")

        @staticmethod
        def complete(
            mac,
            *,
            confirmation,
            display,
            adapter=None,
            compatibility_mode=False,
            explicit_pairing=False,
        ):
            assert compatibility_mode is False
            assert explicit_pairing is False
            return SimpleNamespace(
                device=device,
                ancs_ready=False,
                quirks_report="/tmp/quirks-test.json",
            )

    class Backend:
        @staticmethod
        def status():
            return SimpleNamespace(verified_iphone_setup=())

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(pairing_cli, "BackendClient", Backend)
    monkeypatch.setattr(pairing_cli.typer, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(pairing_cli, "_print_iphone_steps", lambda *_args, **_kwargs: None)

    assert pairing_cli.run_wizard(verify_after=False) == 0
    output = capsys.readouterr().out
    assert "blueferry pairing-issue" in output
    assert "GitHub issue" not in output
    assert "sudo systemctl restart bluetooth.service" in output
