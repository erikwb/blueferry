from __future__ import annotations

from blueferry.onboarding import OnboardingStage
from blueferry.ui.status_presenter import (
    connection_subtitle,
    onboarding_presentation,
)


def test_connection_summary_includes_degraded_detail_and_retry() -> None:
    subtitle = connection_subtitle({
        "connectivity_state": "reconnecting",
        "connectivity_detail": "phone unavailable",
        "retry_delay_seconds": 10,
    }, reachable=True)

    assert "Reconnecting" in subtitle
    assert "phone unavailable" in subtitle
    assert "10s" in subtitle


def test_incompatible_onboarding_uses_probed_reason() -> None:
    title, subtitle, icon = onboarding_presentation(
        OnboardingStage.INCOMPATIBLE,
        incompatibility="Controller lacks BR/EDR",
    )

    assert "Not Compatible" in title
    assert subtitle == "Controller lacks BR/EDR"
    assert icon == "dialog-warning-symbolic"
