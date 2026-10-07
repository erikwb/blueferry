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


def test_gtk_phone_calls_switch_sends_the_choice():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    calls = []
    page = SimpleNamespace(
        _applying_calls_enabled=False,
        _calls_enabled_choice=SavedChoice(),
        _calls_enabled_switch=Mock(get_active=lambda: True),
        _calls_enabled_row=Mock(),
        _client=SimpleNamespace(
            set_calls_enabled_async=lambda enabled, _ok, _err: calls.append(enabled),
        ),
    )

    IPhonePage._calls_enabled_changed(page, None, None)

    assert calls == [True]
    page._calls_enabled_row.set_sensitive.assert_called_with(False)


def test_gtk_phone_calls_switch_ignores_updates_from_a_status_refresh():
    from types import SimpleNamespace

    from blueferry.ui.status import IPhonePage

    # No client or widgets: a refresh must return before touching either.
    page = SimpleNamespace(_applying_calls_enabled=True)

    IPhonePage._calls_enabled_changed(page, None, None)


def test_gtk_phone_calls_switch_holds_the_choice_until_the_save_settles():
    from types import SimpleNamespace

    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    refreshes = []
    saved = {}
    choice = SavedChoice()
    page = SimpleNamespace(
        _applying_calls_enabled=False,
        _calls_enabled_choice=choice,
        _calls_enabled_switch=SimpleNamespace(get_active=lambda: True),
        _calls_enabled_row=SimpleNamespace(set_sensitive=lambda _value: None),
        _client=SimpleNamespace(
            set_calls_enabled_async=lambda _enabled, ok, _err: saved.update(ok=ok),
        ),
        _toast=lambda _text: None,
        _refresh=lambda: refreshes.append(True),
    )

    IPhonePage._calls_enabled_changed(page, None, None)
    # While the save is running, a status still reporting "off" is not shown.
    assert choice.saving and choice.resolve(False) == (True, False)

    saved["ok"]({"calls_enabled": True, "calls_state": "searching"})
    assert not choice.saving and refreshes == [True]
    # One status read before the save finished is skipped and asked again.
    assert choice.resolve(False) == (True, True)
    assert choice.resolve(False) == (False, False)


def test_gtk_phone_calls_switch_reverts_and_reports_a_failed_save():
    from types import SimpleNamespace

    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    toasts = []
    applied = []
    failed = {}
    choice = SavedChoice()
    last = BackendStatus.from_dict({"calls_enabled": False})
    page = SimpleNamespace(
        _applying_calls_enabled=False,
        _calls_enabled_choice=choice,
        _calls_enabled_switch=SimpleNamespace(get_active=lambda: True),
        _calls_enabled_row=SimpleNamespace(set_sensitive=lambda _value: None),
        _last_status=last,
        _client=SimpleNamespace(
            set_calls_enabled_async=lambda _enabled, _ok, err: failed.update(err=err),
        ),
        _toast=toasts.append,
        _apply_status=applied.append,
    )

    IPhonePage._calls_enabled_changed(page, None, None)
    failed["err"]("oFono is missing")

    assert not choice.saving
    assert applied == [last]
    assert toasts == ["Could not save phone calls preference: oFono is missing"]
    assert choice.resolve(False) == (False, False)


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


@pytest.mark.parametrize(("status", "saved", "sensitive"), [
    ({"notification_policy": "all", "notification_content_shown": True}, True, True),
    ({"notification_policy": "all", "notification_content_shown": True}, False, True),
    ({"notification_policy": "messages", "notification_content_shown": True}, False, False),
    ({"notification_policy": "all", "notification_content_shown": False}, False, False),
    # A saved "on" can always be switched off again.
    ({"notification_policy": "messages", "notification_content_shown": True}, True, True),
    ({"notification_policy": "all", "notification_content_shown": False}, True, True),
])
def test_gtk_action_buttons_switch_shows_the_saved_choice_and_when_it_applies(
    status, saved, sensitive
):
    from blueferry.ui.status import IPhonePage

    page = _gtk_actions_page()
    IPhonePage._apply_ancs_actions(
        page,
        BackendStatus.from_dict({**status, "ancs_actions_preference": saved}),
        True,
    )

    page._ancs_actions_row.set_visible.assert_called_with(True)
    page._ancs_actions_switch.set_active.assert_called_with(saved)
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


def test_connection_summary_appends_optional_phone_battery_and_signal() -> None:
    status = {
        "connectivity_state": "ready",
        "phone_battery_level": 40,
        "phone_battery_source": "hfp",
        "phone_signal_strength": 60,
        "phone_network_name": "Sunrise",
        "phone_network_status": "registered",
    }

    assert connection_subtitle(status, reachable=True) == (
        "Ready · Battery about 40 % · Signal 60 %"
    )
    assert connection_subtitle({"connectivity_state": "ready"}, reachable=True) == "Ready"


def _media_switch_page(**overrides):
    from types import SimpleNamespace

    from blueferry.ui.saved_choice import SavedChoice

    class Switch:
        def __init__(self, active=False):
            self.active = active

        def get_active(self):
            return self.active

        def set_active(self, active):
            self.active = active

    class Row:
        sensitive = visible = None

        def set_sensitive(self, value):
            self.sensitive = value

        def set_visible(self, value):
            self.visible = value

    page = SimpleNamespace(
        _applying_saved_switches=False,
        _media_control_choice=SavedChoice(),
        _mpris_player_choice=SavedChoice(),
        _media_control_switch=Switch(),
        _mpris_player_switch=Switch(),
        _media_control_row=Row(),
        _mpris_player_row=Row(),
        _media_group=Row(),
        toasts=[],
        refreshes=[],
        applied=[],
        saves=[],
    )
    page._toast = page.toasts.append
    page._refresh = lambda: page.refreshes.append(True)
    page._apply_status = page.applied.append
    page._client = SimpleNamespace(
        set_media_control_async=lambda enabled, ok, err: page.saves.append(
            ("media", enabled, ok, err)),
        set_mpris_player_async=lambda enabled, ok, err: page.saves.append(
            ("mpris", enabled, ok, err)),
    )
    from blueferry.ui.status import IPhonePage

    for name in ("_show_saved_choice", "_save_switch"):
        setattr(page, name, getattr(IPhonePage, name).__get__(page))
    for name, value in overrides.items():
        setattr(page, name, value)
    return page


def test_gtk_media_switches_follow_the_daemon_and_the_player_needs_media_control():
    from blueferry.ui.status import IPhonePage

    page = _media_switch_page()
    apply = lambda values, reachable=True: IPhonePage._apply_media_switches(  # noqa: E731
        page, BackendStatus.from_dict(values), reachable)

    # Daemons that do not report the keys do not support the settings.
    apply({})
    assert page._media_group.visible is False

    apply({"media_control_enabled": False})
    assert page._media_group.visible is True
    assert page._mpris_player_row.visible is False

    apply({"media_control_enabled": False, "media_mpris_enabled": True})
    assert page._mpris_player_row.visible is True
    assert page._media_control_switch.active is False
    assert page._mpris_player_switch.active is True
    assert page._media_control_row.sensitive is True
    assert page._mpris_player_row.sensitive is False

    apply({"media_control_enabled": True, "media_mpris_enabled": False})
    assert page._media_control_switch.active is True
    assert page._mpris_player_row.sensitive is True
    apply({"media_control_enabled": True, "media_mpris_enabled": False}, reachable=False)
    assert page._media_control_row.sensitive is False
    assert page._mpris_player_row.sensitive is False
    # Setting the switches from a status never saves anything.
    assert page.saves == [] and page._applying_saved_switches is False


def test_gtk_media_switch_ignores_updates_from_a_status_refresh():
    from blueferry.ui.status import IPhonePage

    page = _media_switch_page(_applying_saved_switches=True)
    IPhonePage._media_control_changed(page, page._media_control_switch, None)
    IPhonePage._mpris_player_changed(page, page._mpris_player_switch, None)
    assert page.saves == []


def test_gtk_mpris_switch_holds_the_choice_until_the_save_settles():
    from blueferry.ui.status import IPhonePage

    page = _media_switch_page()
    page._mpris_player_switch.active = True
    IPhonePage._mpris_player_changed(page, page._mpris_player_switch, None)
    choice = page._mpris_player_choice
    assert [(save[0], save[1]) for save in page.saves] == [("mpris", True)]
    assert page._mpris_player_row.sensitive is False
    # While the save is running, a status still reporting "off" is not shown.
    assert choice.saving and choice.resolve(False) == (True, False)

    page.saves[0][2]({"media_mpris_enabled": True, "media_control_enabled": True})
    assert not choice.saving and page.refreshes == [True]
    assert page.toasts == ["Desktop media controls preference saved"]
    # One status read before the save finished is skipped and asked again.
    page._show_saved_choice(page._mpris_player_switch, choice, False)
    assert page._mpris_player_switch.active is True and page.refreshes == [True, True]
    page._show_saved_choice(page._mpris_player_switch, choice, False)
    assert page._mpris_player_switch.active is False and len(page.refreshes) == 2


def test_gtk_media_control_switch_reverts_and_reports_a_failed_save():
    from blueferry.ui.status import IPhonePage

    last = BackendStatus.from_dict({"media_control_enabled": False})
    page = _media_switch_page(_last_status=last)
    page._media_control_switch.active = True
    IPhonePage._media_control_changed(page, page._media_control_switch, None)
    assert [(save[0], save[1]) for save in page.saves] == [("media", True)]

    page.saves[0][3]("no LE link")
    choice = page._media_control_choice
    assert not choice.saving
    assert page.applied == [last]
    assert page.toasts == ["Could not save media control preference: no LE link"]
    assert choice.resolve(False) == (False, False)


def test_gtk_away_lock_switch_opts_in_and_keeps_the_saved_grace_period():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    calls = []
    page = SimpleNamespace(
        _applying_proximity_lock=False,
        _proximity_lock_choice=SavedChoice(),
        _proximity_lock_switch=Mock(get_active=lambda: True),
        _proximity_lock_row=Mock(),
        _last_status=BackendStatus.from_dict({
            "proximity_lock": "off", "proximity_lock_grace_sec": 120,
        }),
        _client=SimpleNamespace(
            set_proximity_lock_async=lambda enabled, grace, _ok, _err: calls.append(
                (enabled, grace)
            ),
        ),
    )

    IPhonePage._proximity_lock_changed(page, None, None)

    assert calls == [(True, 120)]
    page._proximity_lock_row.set_sensitive.assert_called_with(False)


def test_gtk_away_lock_switch_ignores_updates_from_a_status_refresh():
    from types import SimpleNamespace

    from blueferry.ui.status import IPhonePage

    # No client or widgets: a refresh must return before touching either.
    page = SimpleNamespace(_applying_proximity_lock=True)

    IPhonePage._proximity_lock_changed(page, None, None)


def test_gtk_away_lock_switch_holds_the_choice_against_a_stale_status():
    from types import SimpleNamespace

    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    refreshes = []
    saved = {}
    positions = []
    switch = SimpleNamespace(get_active=lambda: True, set_active=positions.append)
    page = SimpleNamespace(
        _applying_proximity_lock=False,
        _proximity_lock_choice=SavedChoice(),
        _proximity_lock_switch=switch,
        _proximity_lock_row=SimpleNamespace(set_sensitive=lambda _value: None),
        _last_status=BackendStatus.from_dict({"proximity_lock": "off"}),
        _client=SimpleNamespace(
            set_proximity_lock_async=lambda _enabled, _grace, ok, _err: saved.update(ok=ok),
        ),
        _toast=lambda _text: None,
        _refresh=lambda: refreshes.append(True),
    )

    def status_reports(reported):
        IPhonePage._show_saved_choice(page, switch, page._proximity_lock_choice, reported)
        return positions[-1]

    IPhonePage._proximity_lock_changed(page, None, None)
    assert page._proximity_lock_choice.saving
    # While the save is running, a status still reporting "off" is not shown.
    assert status_reports(False) is True

    saved["ok"]({"proximity_lock_enabled": True})
    assert not page._proximity_lock_choice.saving
    assert refreshes == [True]
    # One status read before the save finished is skipped and asked again.
    assert status_reports(False) is True
    assert refreshes == [True, True]
    # After that the daemon's report is shown as it is.
    assert status_reports(False) is False
    assert refreshes == [True, True]


def test_saved_choice_follows_the_daemon_once_a_status_confirms_the_save():
    from blueferry.ui.saved_choice import SavedChoice

    choice = SavedChoice()
    assert choice.resolve(True) == (True, False)

    choice.begin(True)
    choice.saved(True)
    # A confirming status needs no second read, and later reports are shown.
    assert choice.resolve(True) == (True, False)
    assert choice.resolve(False) == (False, False)


def test_saved_choice_returns_to_the_report_after_a_failed_save():
    from blueferry.ui.saved_choice import SavedChoice

    choice = SavedChoice()
    choice.begin(True)
    assert choice.saving and choice.resolve(False) == (True, False)

    choice.failed()
    assert not choice.saving
    assert choice.resolve(False) == (False, False)


def _call_history_switch_page():
    from blueferry.ui.saved_choice import SavedChoice
    from blueferry.ui.status import IPhonePage

    page = _media_switch_page()
    switch, row = type(page._media_control_switch), type(page._media_control_row)
    page._call_history_choice = SavedChoice()
    page._missed_call_popups_choice = SavedChoice()
    page._call_history_switch = switch()
    page._missed_call_popups_switch = switch()
    page._call_history_row = row()
    page._missed_call_popups_row = row()
    page._call_history_group = row()
    page._client.set_call_history_async = lambda enabled, popups, ok, err: page.saves.append(
        ("history", enabled, popups, ok, err))
    page.apply = lambda values, reachable=True: IPhonePage._apply_call_history_switches(
        page, BackendStatus.from_dict(values), reachable)
    return page


def test_gtk_call_history_switches_follow_the_daemon():
    page = _call_history_switch_page()

    # Daemons that do not report the keys do not support the settings.
    page.apply({})
    assert page._call_history_group.visible is False

    page.apply({"call_history_enabled": False, "missed_call_notifications": True})
    assert page._call_history_group.visible is True
    assert page._call_history_switch.active is False
    assert page._missed_call_popups_switch.active is True
    assert page._call_history_row.sensitive is True
    # Missed-call popups are only offered while call history is on.
    assert page._missed_call_popups_row.sensitive is False

    page.apply({"call_history_enabled": True, "missed_call_notifications": False})
    assert page._call_history_switch.active is True
    assert page._missed_call_popups_switch.active is False
    assert page._missed_call_popups_row.sensitive is True
    page.apply({"call_history_enabled": True}, reachable=False)
    assert page._call_history_row.sensitive is False
    assert page._missed_call_popups_row.sensitive is False
    # Setting the switches from a status never saves anything.
    assert page.saves == [] and page._applying_saved_switches is False


def test_gtk_call_history_switches_save_both_values_together():
    from blueferry.ui.status import IPhonePage

    page = _call_history_switch_page()
    page.apply({"call_history_enabled": False, "missed_call_notifications": False})
    page._call_history_switch.active = True
    IPhonePage._call_history_changed(page, page._call_history_switch, None)
    assert page.saves[-1][:3] == ("history", True, False)
    assert page._call_history_row.sensitive is False
    choice = page._call_history_choice
    assert choice.saving and choice.resolve(False) == (True, False)

    page.saves[-1][3]({"call_history_enabled": True, "missed_call_notifications": False})
    assert not choice.saving and page.refreshes == [True]
    assert page.toasts == ["Call history preference saved"]

    # The popup switch keeps call history on.
    page._missed_call_popups_switch.active = True
    IPhonePage._missed_call_popups_changed(page, page._missed_call_popups_switch, None)
    assert page.saves[-1][:3] == ("history", True, True)

    page._last_status = BackendStatus.from_dict({"call_history_enabled": True})
    page.saves[-1][4]("storage is locked")
    assert not page._missed_call_popups_choice.saving
    assert page.applied == [page._last_status]
    assert page.toasts[-1] == (
        "Could not save missed call notification preference: storage is locked"
    )


# ---- opt-in tethering (parity with the Qt TetherSection) ---------------------


def _tether(**values):
    from blueferry.tether_status import TetherStatus

    return TetherStatus.from_dict(values)


def test_tether_group_is_hidden_without_tether1():
    from blueferry.ui.status_presenter import tether_controls

    assert tether_controls(None, reachable=True, pending=False).group_visible is False


def test_disabled_tethering_shows_only_the_enable_switch():
    from blueferry.ui.status_presenter import tether_controls

    controls = tether_controls(
        _tether(state="off", enabled=False, autoconnect=True), reachable=True, pending=False,
    )
    assert controls.group_visible is True
    assert controls.enable_active is False
    assert controls.enable_sensitive is True
    assert controls.connect_visible is False
    assert controls.auto_visible is False
    assert "network applet" in controls.summary
    assert controls.warning is False


def test_enabled_tethering_shows_the_connect_and_automatic_switches():
    from blueferry.ui.status_presenter import tether_controls

    controls = tether_controls(
        _tether(state="connected", enabled=True, autoconnect=True),
        reachable=True, pending=False,
    )
    assert (controls.connect_visible, controls.connect_active) == (True, True)
    assert controls.connect_sensitive is True
    assert (controls.auto_visible, controls.auto_active) == (True, True)
    assert "Personal Hotspot" in controls.summary


@pytest.mark.parametrize(("state", "pending", "reachable"), [
    ("connecting", False, True), ("disconnecting", False, True),
    ("off", True, True), ("off", False, False),
])
def test_tether_connect_switch_is_insensitive_while_busy(state, pending, reachable):
    from blueferry.ui.status_presenter import tether_controls

    controls = tether_controls(
        _tether(state=state, enabled=True), reachable=reachable, pending=pending,
    )
    assert controls.connect_sensitive is False
    if pending or not reachable:
        assert controls.enable_sensitive is False
        assert controls.auto_sensitive is False


def test_tether_failure_is_a_warning_only_while_enabled():
    from blueferry.ui.status_presenter import tether_controls

    failed = _tether(state="failed", error="hotspot-refused", enabled=True)
    controls = tether_controls(failed, reachable=True, pending=False)
    assert controls.warning is True
    assert "Personal Hotspot" in controls.summary


class _TetherPage:
    """Borrows IPhonePage's tethering handlers without building widgets."""

    def __init__(self, tether) -> None:
        from types import SimpleNamespace

        self.calls: list[tuple] = []
        self.toasts: list[str] = []
        self.applied: list = []
        self.refreshes = 0
        self._tether = tether
        self._tether_pending = False
        self._applying_tether = False
        self._toast = self.toasts.append
        self._tether_enable_switch = SimpleNamespace(get_active=lambda: True)
        self._tether_connect_switch = SimpleNamespace(get_active=lambda: True)
        self._tether_auto_switch = SimpleNamespace(get_active=lambda: False)
        page = self

        class Client:
            def configure_tether_async(self, enabled, autoconnect, on_ok, on_err):
                page.calls.append(("configure", enabled, autoconnect, on_ok, on_err))

            def set_tether_connected_async(self, connected, on_ok, on_err):
                page.calls.append(("connect", connected, on_ok, on_err))

        self._client = Client()

    def _apply_tether(self, tether):
        self._tether = tether
        self.applied.append((tether, self._tether_pending))

    def _refresh_tether(self):
        self.refreshes += 1


def _borrow():
    from blueferry.ui.status import IPhonePage

    for name in (
        "_tether_request", "_tether_enable_changed",
        "_tether_connect_changed", "_tether_auto_changed",
    ):
        setattr(_TetherPage, name, getattr(IPhonePage, name))


def test_gtk_enable_switch_saves_the_opt_in_and_keeps_automatic_choice():
    _borrow()
    page = _TetherPage(_tether(state="off", enabled=False, autoconnect=True))

    page._tether_enable_changed(None, None)
    page._tether_connect_changed(None, None)  # ignored while pending

    assert [call[:3] for call in page.calls] == [("configure", True, True)]
    assert page.applied[-1][1] is True  # rendered as pending
    page.calls[0][3](_tether(state="off", enabled=True, autoconnect=True))
    assert page._tether_pending is False
    assert page._tether.enabled is True


def test_gtk_programmatic_updates_do_not_send_requests():
    _borrow()
    page = _TetherPage(_tether(state="off", enabled=True))
    page._applying_tether = True
    page._tether_enable_changed(None, None)
    page._tether_connect_changed(None, None)
    page._tether_auto_changed(None, None)
    assert page.calls == []


def test_gtk_connect_and_automatic_switches_send_explicit_requests():
    _borrow()
    page = _TetherPage(_tether(state="off", enabled=True))
    page._tether_connect_changed(None, None)
    assert page.calls[0][:2] == ("connect", True)
    page.calls[0][2](_tether(state="connecting", enabled=True))
    page._tether_auto_changed(None, None)
    assert page.calls[1][:3] == ("configure", True, False)


def test_gtk_refused_tether_request_toasts_and_refetches():
    _borrow()
    page = _TetherPage(_tether(state="off", enabled=False))
    page._tether_connect_changed(None, None)
    page.calls[0][3]("Bluetooth tethering is turned off")
    assert page._tether_pending is False
    assert "turned off" in page.toasts[0]
    assert page.refreshes == 1
