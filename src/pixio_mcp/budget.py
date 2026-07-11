"""Credit-spend guardrails for pixio-mcp.

Implements :class:`BudgetGuard`, the server-side enforcement of the two
non-negotiable spend caps (see PRD D3 / CONTRACTS.md "Budget guard"):

* a per-job cap (``PIXIO_MAX_CREDITS_PER_JOB``), and
* a cumulative per-session budget (``PIXIO_SESSION_BUDGET``).

Estimates are checked *before* any ``POST /generate`` is issued; submitted
estimates are recorded per generation id and later reconciled against the
actual ``creditsCost`` reported by the API, so ``session_spent`` converges on
real spend without double-counting.
"""

from __future__ import annotations

from uuid import uuid4

from .errors import ErrorCode, PixioError

__all__ = ["BudgetGuard"]


def _coerce_credits(value: int | float | None) -> int:
    """Coerce a credit amount to a non-negative int.

    ``None`` and negative or unparseable values collapse to ``0`` so that a
    malformed upstream estimate can never corrupt budget accounting or raise
    from inside the guard itself.
    """
    if value is None:
        return 0
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, amount)


class BudgetGuard:
    """Tracks session credit spend and enforces per-job / per-session caps.

    The guard is purely in-memory (one instance per server process, held on
    the :class:`~pixio_mcp.runtime.Runtime`). All amounts are credits.
    """

    def __init__(self, max_per_job: int, session_budget: int) -> None:
        """Create a guard with the two caps.

        Args:
            max_per_job: Maximum estimated credits allowed for a single job
                without an explicit ``confirm=True`` override.
            session_budget: Maximum cumulative credits allowed across the
                whole server session without an explicit override.
        """
        self._max_per_job = _coerce_credits(max_per_job)
        self._session_budget = _coerce_credits(session_budget)
        self._spent = 0
        # Per-generation-id recorded amounts; used by record_actual to
        # reconcile estimates against real creditsCost idempotently.
        self._recorded: dict[str, int] = {}

    @property
    def session_spent(self) -> int:
        """Total credits recorded for this session, clamped at >= 0."""
        return max(0, self._spent)

    def check(self, estimated: int, confirm: bool) -> None:
        """Refuse a job whose estimate would breach a cap, unless confirmed.

        Raises :class:`PixioError` with code ``BUDGET_EXCEEDED`` when
        ``estimated`` exceeds the per-job cap, or when
        ``session_spent + estimated`` exceeds the session budget. The per-job
        cap is checked first and the error reports whichever cap tripped.
        With ``confirm=True`` this method never raises.

        Args:
            estimated: Estimated credit cost of the prospective job
                (``None``/negative values are treated as 0).
            confirm: Explicit caller override; ``True`` bypasses both caps.

        Raises:
            PixioError: ``BUDGET_EXCEEDED`` describing the estimate, the cap
                that tripped, the cap value, the current session spend, and
                the ``confirm=true`` override hint.
        """
        if confirm:
            return
        estimate = _coerce_credits(estimated)
        spent = self.session_spent

        if estimate > self._max_per_job:
            cap_description = (
                f"exceeds the per-job cap of {self._max_per_job} credits"
            )
        elif spent + estimate > self._session_budget:
            cap_description = (
                f"would exceed the session budget of "
                f"{self._session_budget} credits"
            )
        else:
            return

        raise PixioError(
            ErrorCode.BUDGET_EXCEEDED,
            (
                f"Estimated cost of {estimate} credits {cap_description} "
                f"(session_spent so far: {spent} credits); "
                f"pass confirm=true to override."
            ),
            details={
                "estimated_credits": estimate,
                "per_job_cap": self._max_per_job,
                "session_budget": self._session_budget,
                "session_spent": spent,
            },
        )

    def reserve(self, estimated: int, confirm: bool) -> str:
        """Atomically check the caps and provisionally record the estimate.

        Combines :meth:`check` and a provisional :meth:`record_submit` in one
        synchronous step (no ``await`` in between), so concurrent async tool
        calls each see the spend of every reservation made before theirs —
        parallel ``generate()`` calls cannot collectively overspend the
        session budget by racing between check and record.

        Args:
            estimated: Estimated credit cost of the prospective job.
            confirm: Explicit caller override; ``True`` bypasses both caps.

        Returns:
            An opaque reservation token. After the job is submitted, call
            :meth:`commit` to re-key the reservation under the real
            generation id; if submission fails, call :meth:`release` to
            return the reserved credits to the session budget.

        Raises:
            PixioError: ``BUDGET_EXCEEDED`` exactly as :meth:`check` (nothing
                is reserved when this raises).
        """
        self.check(estimated, confirm)
        token = f"__reserved__{uuid4().hex}"
        self.record_submit(token, estimated)
        return token

    def commit(self, token: str, generation_id: str) -> None:
        """Re-key a reservation made by :meth:`reserve` under the real id.

        The session total is unchanged (the amount was counted at reserve
        time); the per-id record moves from *token* to ``generation_id`` so a
        later :meth:`record_actual` reconciles correctly. Unknown tokens
        commit 0 credits.
        """
        amount = self._recorded.pop(token, 0)
        self._recorded[generation_id] = self._recorded.get(generation_id, 0) + amount

    def release(self, token: str) -> None:
        """Return a reservation's credits to the budget (submission failed).

        Subtracts the amount reserved under *token* from the session total
        and forgets the token. Unknown tokens are a no-op.
        """
        amount = self._recorded.pop(token, 0)
        self._spent = max(0, self._spent - amount)

    def record_submit(self, generation_id: str, estimated: int) -> None:
        """Record the estimated cost of a job that was just submitted.

        Adds ``estimated`` to the session total and remembers the amount
        under ``generation_id`` so a later :meth:`record_actual` can
        reconcile it against the real cost.

        Args:
            generation_id: The ``contentId`` returned by ``POST /generate``.
            estimated: Estimated credit cost recorded at submission time
                (``None``/negative values are treated as 0).
        """
        estimate = _coerce_credits(estimated)
        self._recorded[generation_id] = estimate
        self._spent = max(0, self._spent + estimate)

    def record_actual(self, generation_id: str, actual: int) -> None:
        """Reconcile a job's recorded spend against its actual cost.

        Adjusts the session total by the delta between ``actual`` and the
        amount previously recorded for ``generation_id`` (0 for an unknown
        id, so an unknown id simply adds ``actual``), then updates the
        per-id record. Idempotent: repeating the call with the same
        ``actual`` changes nothing.

        Args:
            generation_id: The generation id whose terminal ``creditsCost``
                was observed.
            actual: Actual credits charged (``None``/negative values are
                treated as 0).
        """
        amount = _coerce_credits(actual)
        previous = self._recorded.get(generation_id, 0)
        self._spent = max(0, self._spent + (amount - previous))
        self._recorded[generation_id] = amount
