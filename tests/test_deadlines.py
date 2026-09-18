"""Track A6: §24.1 deadline computation — allowances, formula, budget."""
from datetime import datetime, timedelta, timezone

from orchestrator.deadlines import (
    wan_allowance_seconds, compute_absolute_deadline,
    remaining_budget_seconds,
)
from orchestrator.config_loader import load_v2_baseline


def test_allowance_pair_specific_and_default():
    base = load_v2_baseline()
    assert wan_allowance_seconds("us-east-1", "us-west-2", "us-east-1", base) == 20.0
    assert wan_allowance_seconds("eu-west-1", "ap-south-1", "us-east-1", base) == 15.0
    # Order-insensitive.
    assert wan_allowance_seconds("us-west-2", "us-east-1", "us-east-1", base) == 20.0


def test_absolute_deadline_formula():
    base = load_v2_baseline()
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    ddl = compute_absolute_deadline(t0, 120.0, "us-east-1", "us-west-2",
                                    "us-east-1", base)
    # 120 window − 20 pair allowance − 5 skew.
    assert ddl == t0 + timedelta(seconds=95.0)


def test_remaining_budget_counts_down():
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    ddl = t0 + timedelta(seconds=95.0)
    assert remaining_budget_seconds(ddl, t0 + timedelta(seconds=10)) == 85.0
    assert remaining_budget_seconds(None) is None
    assert remaining_budget_seconds(ddl, t0 + timedelta(seconds=200)) < 0
