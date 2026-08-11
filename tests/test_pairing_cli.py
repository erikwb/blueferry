from types import SimpleNamespace

from blueferry import pairing_cli
from blueferry.bluetooth_devices import PairedDevice
from blueferry.pairing_cli import _print_iphone_steps
from blueferry.setup_verification import CONTACTS


def test_verified_cli_setup_omits_the_iphone_section(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(),
        notifications_supported=True,
        ancs_ready=True,
    )

    assert capsys.readouterr().out == ""


def test_cli_setup_always_prints_required_bluetooth_toggles(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(CONTACTS,),
        notifications_supported=True,
        ancs_ready=True,
    )

    output = capsys.readouterr().out
    assert "3. Allow Notification Access when prompted" in output
    assert "4. Toggle on Show Message Notifications and Sync Contacts" in output
    assert "back out and tap the (i) again" in output
    assert "Sync Contacts" in output
    assert "Show Message Notifications" in output


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


def test_cli_requires_phone_side_forget_before_clearing_saved_target(
    monkeypatch,
    capsys,
) -> None:
    prompts = []
    forgotten = []

    class Setup:
        @staticmethod
        def configuration():
            return SimpleNamespace(saved=True, mac="02:00:00:00:00:01")

        @staticmethod
        def forget(mac):
            forgotten.append(mac)
            raise pairing_cli.PairingError("stop after forget")

    monkeypatch.setattr(pairing_cli, "SetupClient", Setup)
    monkeypatch.setattr(
        pairing_cli.typer,
        "confirm",
        lambda prompt, **kwargs: prompts.append((prompt, kwargs)) or True,
    )

    assert pairing_cli.run_wizard(verify_after=False) == 1
    assert forgotten == ["02:00:00:00:00:01"]
    assert prompts == [
        (
            "Forget BlueFerry's configured target and start fresh?",
            {"default": False},
        )
    ]
    output = capsys.readouterr().out
    assert "Before answering Yes, forget this PC on the iPhone too" in output
    assert "Forget This Device" in output


def test_cli_wizard_supplies_its_own_pairing_agent_ui(monkeypatch, capsys) -> None:
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
        hardware_supported=True,
        issue="",
        notifications_supported=True,
        bearer_api_active=True,
    )

    class Setup:
        @staticmethod
        def compatibility():
            return compatibility

        @staticmethod
        def devices(*, scan_seconds):
            assert scan_seconds == 8
            return [device]

        @staticmethod
        def configuration():
            return SimpleNamespace(saved=False, mac="")

        @staticmethod
        def complete(mac, *, confirmation, display):
            observed.append((mac, confirmation(12345)))
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

    assert pairing_cli.run_wizard(verify_after=False) == 0
    assert observed == [(device.mac, True)]
    assert prompts == [
        ("\nUse this device?", {"default": True}),
        ("Do both devices show Bluetooth code 012345?", {"default": False}),
    ]
    assert "Bluetooth pairing code: 012345" in capsys.readouterr().out
