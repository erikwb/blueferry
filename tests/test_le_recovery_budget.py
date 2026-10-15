"""Recovery cannot turn a flapping phone into an unbounded dial/reset loop."""
from blueferry.le_recovery_budget import LeRecoveryBudget


def test_rolling_limit_and_cooldown():
    now = 0.0
    budget = LeRecoveryBudget(lambda: now)
    for allowed, next_time in ((0, 30), (30, 90), (90, 210), (210, 600)):
        now = allowed
        assert budget.take()
        now = next_time - 0.01
        assert not budget.take()
    now = 600
    assert budget.take()
    assert not budget.take()


def test_only_sustained_fresh_health_forgives_budget():
    now = 0.0
    budget = LeRecoveryBudget(lambda: now)
    for now in (0, 30, 90, 210):  # noqa: B007 - read through the injected clock
        assert budget.take()
    for now in (220, 280):
        budget.observe_health(True, now)
    budget.observe_health(False, None)
    for now in (290, 350, 410):
        budget.observe_health(True, now)
        assert not budget.take()
    now = 470
    budget.observe_health(True, now)
    assert budget.take()


def test_old_proof_and_missing_observations_do_not_forgive_budget():
    now = 0.0
    budget = LeRecoveryBudget(lambda: now)
    for now in (0, 30, 90, 210):  # noqa: B007 - read through the injected clock
        assert budget.take()
    budget.observe_health(True, 210)
    now = 400
    budget.observe_health(True, now)  # A long unobserved gap starts a new dwell.
    assert not budget.take()
    now = 490
    budget.observe_health(True, 210)  # Connected=true alone proves nothing.
    assert not budget.take()
    now = 580
    budget.observe_health(True, now)
    assert not budget.take()
