"""Unit tests for pixio_mcp.budget.BudgetGuard (contract B3)."""

from __future__ import annotations

import pytest

from pixio_mcp.budget import BudgetGuard
from pixio_mcp.errors import ErrorCode, PixioError


def _trip(guard: BudgetGuard, estimated: int) -> PixioError:
    """Run guard.check without confirm and return the raised PixioError."""
    with pytest.raises(PixioError) as excinfo:
        guard.check(estimated, confirm=False)
    return excinfo.value


def test_per_job_cap_trip_message_and_details() -> None:
    # AC-3 (unit slice): estimate over the per-job cap is refused with the
    # estimate, the cap value, and the confirm=true override hint.
    guard = BudgetGuard(60, 300)
    err = _trip(guard, 100)
    assert err.code == ErrorCode.BUDGET_EXCEEDED
    inner = err.to_dict()["error"]
    assert "100" in inner["message"]
    assert "60" in inner["message"]
    assert "confirm=true" in inner["message"].lower()
    assert inner["details"]["estimated_credits"] == 100
    assert inner["details"]["per_job_cap"] == 60
    assert inner["details"]["session_budget"] == 300
    assert inner["details"]["session_spent"] == 0


def test_session_budget_trip() -> None:
    guard = BudgetGuard(60, 300)
    for i in range(5):
        guard.record_submit(f"gen-{i}", 55)
    assert guard.session_spent == 275
    err = _trip(guard, 50)  # 50 <= per-job cap, but 275 + 50 > 300
    assert err.code == ErrorCode.BUDGET_EXCEEDED
    inner = err.to_dict()["error"]
    assert "50" in inner["message"]
    assert "300" in inner["message"]
    assert "confirm=true" in inner["message"].lower()
    assert inner["details"]["estimated_credits"] == 50
    assert inner["details"]["session_spent"] == 275


def test_per_job_cap_reported_first_when_both_trip() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_submit("gen-a", 280)
    err = _trip(guard, 90)  # trips both caps; per-job must be reported
    assert err.code == ErrorCode.BUDGET_EXCEEDED
    inner = err.to_dict()["error"]
    assert "60" in inner["message"]
    assert inner["details"]["estimated_credits"] == 90


def test_at_cap_boundaries_do_not_trip() -> None:
    guard = BudgetGuard(60, 300)
    guard.check(60, confirm=False)  # estimated == per-job cap: allowed
    guard.record_submit("gen-a", 240)
    guard.check(60, confirm=False)  # 240 + 60 == session budget: allowed


def test_confirm_true_never_raises() -> None:
    guard = BudgetGuard(60, 300)
    guard.check(10_000, confirm=True)  # over per-job cap
    guard.record_submit("gen-big", 10_000)  # session budget blown
    guard.check(10_000, confirm=True)  # over both caps


def test_record_submit_accumulates() -> None:
    guard = BudgetGuard(60, 300)
    assert guard.session_spent == 0
    guard.record_submit("gen-a", 10)
    guard.record_submit("gen-b", 5)
    assert guard.session_spent == 15


def test_record_actual_reconciles_estimate_down() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_submit("gen-a", 10)
    assert guard.session_spent == 10
    guard.record_actual("gen-a", 4)
    assert guard.session_spent == 4


def test_record_actual_is_idempotent() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_submit("gen-a", 10)
    guard.record_actual("gen-a", 4)
    guard.record_actual("gen-a", 4)  # same actual again: no change
    assert guard.session_spent == 4


def test_record_actual_updates_on_new_actual() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_submit("gen-a", 10)
    guard.record_actual("gen-a", 4)
    guard.record_actual("gen-a", 6)  # revised actual replaces the old one
    assert guard.session_spent == 6


def test_record_actual_unknown_id_adds_actual() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_actual("gen-mystery", 7)
    assert guard.session_spent == 7
    guard.record_actual("gen-mystery", 7)  # now known: idempotent
    assert guard.session_spent == 7


def test_session_spent_clamps_at_zero() -> None:
    guard = BudgetGuard(60, 300)
    guard.record_submit("gen-a", 10)
    guard.record_actual("gen-a", 0)
    assert guard.session_spent == 0
    guard.record_actual("gen-weird", -5)  # defensive: never goes negative
    assert guard.session_spent == 0
