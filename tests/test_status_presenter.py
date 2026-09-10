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
