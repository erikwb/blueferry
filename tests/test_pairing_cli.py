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


def test_cli_setup_prints_only_remaining_tasks(capsys) -> None:
    _print_iphone_steps(
        frozenset(),
        remaining=(CONTACTS,),
        notifications_supported=True,
        ancs_ready=True,
    )

    output = capsys.readouterr().out
    assert "Sync Contacts" in output
    assert "Show Message Notifications" not in output
    assert "Notification Access" not in output
