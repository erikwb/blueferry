from __future__ import annotations

import pytest

from blueferry.models import BackendStatus
from blueferry.protocol import backend_compatibility_error
from blueferry.ui.status_presenter import (
    connection_subtitle,
    map_connection_refused,
    map_connection_refused_message,
)


def test_backend_incompatibility_is_preserved_on_the_status_page():
    from types import SimpleNamespace

    from blueferry.ui.status import IPhonePage

    rendered = []
    page = SimpleNamespace(_apply_status=rendered.append)
    message = backend_compatibility_error({})
    IPhonePage._status_failed(page, message)
    assert connection_subtitle(rendered[0].to_dict(), reachable=False) == message
    assert "incompatible" not in connection_subtitle(BackendStatus().to_dict(), reachable=False)


def test_gtk_pairing_blocks_incompatible_hardware_but_allows_unverified_hardware():
    from types import SimpleNamespace

    from blueferry.setup_client import BluetoothCompatibility
    from blueferry.ui.status import IPhonePage

    enabled = []
    widget = SimpleNamespace(set_sensitive=lambda _value: None, set_spinning=lambda _value: None)
    page = SimpleNamespace(
        _setup_spinner=widget, _activate_button=widget, _scan_button=widget,
        _adapter_row=widget, _compatibility_switch=widget, _explicit_pairing_switch=widget,
        _forget_button=widget,
        _pair_button=SimpleNamespace(set_sensitive=enabled.append, set_label=lambda _label: None),
        _selected_device=lambda: SimpleNamespace(paired=True),
        _update_phone_controls=lambda: None,
        _compatibility=None,
    )
    for available, pairing_ready in ((True, False), (False, True), (True, True)):
        page._compatibility = BluetoothCompatibility.from_dict({
            "available": available, "pairing_ready": pairing_ready,
            "notifications_supported": False,
        })
        IPhonePage._set_pairing_busy(page, False)
    assert enabled == [False, True, True]
    IPhonePage._set_pairing_busy(page, True)
    assert enabled[-1] is False


@pytest.mark.parametrize("available,pairing_ready", [(True, False), (False, True), (True, True)])
def test_gtk_configured_phone_surfaces_incompatibility_instead_of_permission_tasks(
    available, pairing_ready,
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from blueferry.setup_client import BluetoothCompatibility, ConfigurationState
    from blueferry.ui.status import IPhonePage

    page = SimpleNamespace(
        _configuration=ConfigurationState.from_dict({
            "configured": True, "saved": True, "mac": "OLD", "ancs_enabled": False,
        }),
        _compatibility=BluetoothCompatibility.from_dict({
            "available": available, "pairing_ready": pairing_ready,
            "notifications_supported": False,
        }),
        _last_status=BackendStatus(daemon=True),
        _hardware_group=Mock(), _pairing_group=Mock(), _paired_group=Mock(),
        _paired_row=Mock(), _unpair_button=Mock(),
        _setup_spinner=Mock(get_spinning=lambda: False),
        _configured_device=lambda: None,
        _iphone_setup_rows={key: Mock() for key in ("message-notifications", "contacts")},
        _iphone_setup_group=Mock(),
    )
    page._update_iphone_setup_tasks = lambda: IPhonePage._update_iphone_setup_tasks(page)
    IPhonePage._update_phone_controls(page)

    page._hardware_group.set_visible.assert_called_with(not pairing_ready)
    page._pairing_group.set_visible.assert_called_with(False)
    page._paired_group.set_visible.assert_called_with(True)
    page._iphone_setup_group.set_visible.assert_called_with(pairing_ready)
    for row in page._iphone_setup_rows.values():
        row.set_visible.assert_called_with(pairing_ready)


def test_connection_summary_includes_degraded_detail_and_retry() -> None:
    subtitle = connection_subtitle(
        {
            "connectivity_state": "reconnecting",
            "connectivity_detail": "phone unavailable",
            "retry_delay_seconds": 10,
        },
        reachable=True,
    )

    assert "Reconnecting" in subtitle
    assert "phone unavailable" in subtitle
    assert "10s" in subtitle


def test_map_refusal_has_a_specific_user_facing_explanation() -> None:
    status = {
        "connectivity_state": "map-connection-refused",
        "connectivity_detail": (
            "CreateSession(MAP) failed: org.bluez.obex.Error.Failed: "
            "Connection refused (111)"
        ),
        "retry_delay_seconds": 15,
    }

    assert map_connection_refused(status) is True
    assert map_connection_refused_message() == (
        "iPhone is refusing message connections; is it connected to another computer?"
    )
    assert "Connection refused (111)" in connection_subtitle(status, reachable=True)


def test_legacy_degraded_status_still_recognizes_errno_111() -> None:
    assert map_connection_refused(
        {
            "connectivity_state": "degraded",
            "connectivity_detail": "CreateSession(MAP) failed: Connection refused (111)",
        }
    ) is True
    assert map_connection_refused(
        {
            "connectivity_state": "degraded",
            "connectivity_detail": "CreateSession(PBAP) failed: Connection refused (111)",
        }
    ) is False


def _gtk_actions_page():
    from types import SimpleNamespace
    from unittest.mock import Mock

    return SimpleNamespace(
        _applying_ancs_actions=False,
        _ancs_actions_row=Mock(),
        _ancs_actions_switch=Mock(),
    )


def test_gtk_action_buttons_switch_is_hidden_for_daemons_without_the_setting():
    from blueferry.ui.status import IPhonePage

    page = _gtk_actions_page()
    IPhonePage._apply_ancs_actions(
        page, BackendStatus.from_dict({"notification_policy": "all"}), True
    )

    page._ancs_actions_row.set_visible.assert_called_with(False)
    page._ancs_actions_switch.set_active.assert_called_with(False)
    assert page._applying_ancs_actions is False


@pytest.mark.parametrize(("status", "sensitive"), [
    ({"notification_policy": "all", "notification_content_shown": True}, True),
    ({"notification_policy": "messages", "notification_content_shown": True}, False),
    ({"notification_policy": "all", "notification_content_shown": False}, False),
])
def test_gtk_action_buttons_switch_shows_the_saved_choice_and_when_it_applies(
    status, sensitive
):
    from blueferry.ui.status import IPhonePage

    page = _gtk_actions_page()
    IPhonePage._apply_ancs_actions(
        page,
        BackendStatus.from_dict({**status, "ancs_actions_preference": True}),
        True,
    )

    page._ancs_actions_row.set_visible.assert_called_with(True)
    page._ancs_actions_switch.set_active.assert_called_with(True)
    page._ancs_actions_row.set_sensitive.assert_called_with(sensitive)
    # The switch is set while _applying is on, so it never echoes a save.
    assert page._applying_ancs_actions is False


def test_gtk_action_buttons_switch_is_inactive_while_the_daemon_is_unreachable():
    from blueferry.ui.status import IPhonePage

    page = _gtk_actions_page()
    IPhonePage._apply_ancs_actions(
        page,
        BackendStatus.from_dict({
            "notification_policy": "all", "ancs_actions_preference": False,
        }),
        False,
    )

    page._ancs_actions_row.set_sensitive.assert_called_with(False)


def test_gtk_action_buttons_switch_saves_the_choice():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from blueferry.ui.status import IPhonePage

    calls = []
    toasts = []
    refreshes = []
    page = SimpleNamespace(
        _applying_ancs_actions=False,
        _ancs_actions_switch=Mock(get_active=lambda: True),
        _ancs_actions_row=Mock(),
        _client=SimpleNamespace(
            set_ancs_notification_actions_async=lambda enabled, ok, _err: (
                calls.append(enabled), ok(enabled)
            ),
        ),
        _toast=toasts.append,
        _refresh=lambda: refreshes.append(True),
    )

    IPhonePage._ancs_actions_changed(page, None, None)

    assert calls == [True]
    page._ancs_actions_row.set_sensitive.assert_called_with(False)
    assert toasts == ["Action button preference saved"]
    assert refreshes == [True]


def test_gtk_action_buttons_switch_restores_the_status_after_a_failed_save():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from blueferry.ui.status import IPhonePage

    rendered = []
    toasts = []
    last = BackendStatus.from_dict({"ancs_actions_preference": False})
    page = SimpleNamespace(
        _applying_ancs_actions=False,
        _ancs_actions_switch=Mock(get_active=lambda: True),
        _ancs_actions_row=Mock(),
        _last_status=last,
        _client=SimpleNamespace(
            set_ancs_notification_actions_async=lambda _enabled, _ok, err: err(
                "denied"
            ),
        ),
        _toast=toasts.append,
        _apply_status=rendered.append,
    )

    IPhonePage._ancs_actions_changed(page, None, None)

    assert rendered == [last]
    assert toasts == ["Could not save action button preference: denied"]


def test_gtk_action_buttons_switch_ignores_updates_from_a_status_refresh():
    from types import SimpleNamespace

    from blueferry.ui.status import IPhonePage

    # No client or widgets: a refresh must return before touching either.
    page = SimpleNamespace(_applying_ancs_actions=True)

    IPhonePage._ancs_actions_changed(page, None, None)
