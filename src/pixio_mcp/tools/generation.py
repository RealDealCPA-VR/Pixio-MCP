"""Generation lifecycle tools: generate, get_generation, wait_for_generation.

These tools drive the Pixio job lifecycle — submit (``POST /generate``),
poll (``GET /generations/{id}``), and surface results in the common
job-result shape — with credit guardrails enforced before any spend.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from pixio_mcp.errors import ErrorCode, PixioError, tool_guard
from pixio_mcp.pathguard import find_local_paths
from pixio_mcp.runtime import get_runtime
from pixio_mcp.tools.credits import resolve_estimate

__all__ = ["generate", "get_generation", "wait_for_generation"]

logger = logging.getLogger(__name__)

_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"

_POLL_INITIAL_S = 2.0
_POLL_FACTOR = 1.5
_POLL_CAP_S = 10.0
_POLL_JITTER = 0.2
_MIN_SLEEP_S = 0.05


def _as_int(value: Any) -> int | None:
    """Coerce an API-provided value to int, returning None when impossible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_output_urls(record: dict) -> list[str]:
    """Ordered-unique output URLs: ``outputUrl`` first, then http(s) values
    from the ``outputs`` object."""
    urls: list[str] = []
    primary = record.get("outputUrl")
    if isinstance(primary, str) and primary:
        urls.append(primary)
    outputs = record.get("outputs")
    if isinstance(outputs, dict):
        for value in outputs.values():
            if (
                isinstance(value, str)
                and value.startswith(("http://", "https://"))
                and value not in urls
            ):
                urls.append(value)
    return urls


def _job_result(
    record: dict,
    generation_id: str,
    *,
    remaining_balance: int | None,
    elapsed_s: float,
) -> dict:
    """Build the common job-result dict from a raw generation record."""
    outputs = record.get("outputs")
    return {
        "generation_id": generation_id,
        "status": record.get("status") or "unknown",
        "output_urls": _extract_output_urls(record),
        "outputs": outputs if isinstance(outputs, dict) else {},
        "model_id": record.get("modelId") or "",
        "credits_spent": _as_int(record.get("creditsCost")),
        "remaining_balance": remaining_balance,
        "elapsed_s": elapsed_s,
        "error": record.get("error"),
    }


async def _fetch_balance() -> int | None:
    """Best-effort account balance (``GET /credits`` total); None on failure."""
    rt = get_runtime()
    try:
        credits = await rt.client.get_credits()
    except Exception:  # noqa: BLE001 - balance is best-effort decoration only
        return None
    if not isinstance(credits, dict):
        return None
    return _as_int(credits.get("total"))


async def _poll(generation_id: str, timeout_s: float) -> dict:
    """Poll a generation until terminal status or deadline.

    Backoff: interval starts at 2.0s, multiplied by 1.5 each cycle, capped
    at 10.0s, with +/-20% random jitter on every sleep. Sleeps never
    overshoot the deadline by design; the status is always checked at least
    once before a timeout can be reported.

    Returns the job-result dict on ``succeeded`` (budget actuals recorded,
    balance fetched best-effort). Raises ``PixioError(GENERATION_FAILED)``
    on ``failed`` (after reconciling budget actuals) and
    ``PixioError(TIMEOUT_PENDING)`` when the deadline passes while the job
    is still non-terminal.
    """
    rt = get_runtime()
    started = time.monotonic()
    deadline = started + timeout_s
    interval = _POLL_INITIAL_S
    while True:
        try:
            record = await rt.client.get_generation(generation_id)
        except PixioError as err:
            # Credits may already be committed for this job and Pixio has no
            # list-generations endpoint — the id MUST survive into the error
            # result or the caller permanently loses access to the job.
            err.details.setdefault("generation_id", generation_id)
            err.details.setdefault(
                "hint",
                "the job may still be running server-side; call "
                "wait_for_generation(generation_id) to resume waiting",
            )
            raise
        status = record.get("status")
        if status == _STATUS_SUCCEEDED:
            actual = _as_int(record.get("creditsCost"))
            if actual is not None:
                rt.budget.record_actual(generation_id, actual)
            balance = await _fetch_balance()
            logger.info(
                "generation %s succeeded (credits_spent=%s)", generation_id, actual
            )
            return _job_result(
                record,
                generation_id,
                remaining_balance=balance,
                elapsed_s=round(time.monotonic() - started, 3),
            )
        if status == _STATUS_FAILED:
            actual = _as_int(record.get("creditsCost"))
            rt.budget.record_actual(generation_id, actual if actual is not None else 0)
            reason = record.get("error")
            logger.info("generation %s failed: %s", generation_id, reason)
            raise PixioError(
                ErrorCode.GENERATION_FAILED,
                f"Generation {generation_id} failed: "
                f"{reason or 'no reason provided by provider'}",
                details={
                    "generation_id": generation_id,
                    "provider_reason": reason,
                },
            )
        now = time.monotonic()
        if now >= deadline:
            raise PixioError(
                ErrorCode.TIMEOUT_PENDING,
                f"Generation {generation_id} is still "
                f"{status or 'processing'} after {timeout_s:g}s. The job keeps "
                "running server-side — call "
                "wait_for_generation(generation_id) to resume waiting.",
                details={
                    "generation_id": generation_id,
                    "timeout_s": timeout_s,
                    "hint": "call wait_for_generation(generation_id) to resume",
                },
            )
        delay = min(
            interval * random.uniform(1.0 - _POLL_JITTER, 1.0 + _POLL_JITTER),
            max(deadline - now, _MIN_SLEEP_S),
        )
        await asyncio.sleep(delay)
        interval = min(interval * _POLL_FACTOR, _POLL_CAP_S)


@tool_guard
async def generate(
    model_id: str,
    params: dict,
    wait: bool = True,
    timeout_s: int | None = None,
    confirm: bool = False,
) -> dict:
    """Run a media generation job on Pixio, with spend guardrails.

    This is the final step of the 3-call discovery contract:
    ``list_models`` -> ``get_model_params`` -> ``generate``. Build ``params``
    from the live ``get_model_params(model_id)`` response — this server
    embeds no model schemas. On a first attempt send EVERY param the schema
    lists, at its default value (some params marked optional are actually
    required by the gateway), and send select-option values as STRINGS
    (e.g. "5", not 5).

    URLs-only contract: every media input inside ``params`` must be an
    http(s) or data: URL. Any value that looks like a local filesystem path
    (``~``, ``./``, ``../``, ``file://``, ``X:\\``, UNC ``\\\\``, or an
    existing file) is rejected with a VALIDATION error naming the offending
    field(s) — before any credits are spent. Call ``upload_media`` first and
    pass the permanent URL it returns.

    Spend guardrails: the job cost is estimated up front and refused with
    BUDGET_EXCEEDED if it exceeds the per-job cap or would exceed the
    session budget — no credits are spent on a refusal. Pass ``confirm=true``
    to explicitly override both caps for this one job.

    Waiting: with ``wait=true`` (default) this call polls until the job is
    terminal or ``timeout_s`` elapses (default: PIXIO_DEFAULT_TIMEOUT_S,
    180s). On timeout you get a TIMEOUT_PENDING error whose details carry
    the ``generation_id`` — the job KEEPS RUNNING server-side; resume with
    ``wait_for_generation(generation_id)``. With ``wait=false`` the call
    returns immediately (status "processing", plus ``estimated_credits``);
    check later with ``get_generation`` or ``wait_for_generation``.

    Output URLs may be signed and expire after roughly an hour — call
    ``download_output(generation_id)`` promptly.

    Args:
        model_id: Pixio model id, e.g. "pixio/flux-1/schnell".
        params: Generation inputs built from ``get_model_params`` (URLs only
            for media fields).
        wait: Poll to completion (True, default) or return immediately.
        timeout_s: Max seconds to wait when ``wait=true``; None uses the
            server default.
        confirm: Set True to override the per-job and session credit caps.

    Returns:
        On success: {"generation_id", "status", "output_urls", "outputs",
        "model_id", "credits_spent", "remaining_balance", "elapsed_s",
        "error"}. With ``wait=false``: the same shape with status
        "processing", ``credits_spent`` None, and ``estimated_credits``
        added. On failure: {"error": {"code", "message", "details"}} with
        code VALIDATION, BUDGET_EXCEEDED, INSUFFICIENT_CREDITS, CONCURRENCY,
        GENERATION_FAILED, TIMEOUT_PENDING, NOT_FOUND, AUTH, or
        UPSTREAM_ERROR.
    """
    started = time.monotonic()

    # Step 1 — URLs-only enforcement, before any network call or spend.
    hits = find_local_paths(params)
    if hits:
        fields = [f"params.{path}" for path, _ in hits]
        noun = (
            "looks like a local file path"
            if len(fields) == 1
            else "look like local file paths"
        )
        raise PixioError(
            ErrorCode.VALIDATION,
            f"{', '.join(fields)} {noun}; run upload_media first — "
            "generate accepts URLs only",
            details={
                "fields": fields,
                "values": {f"params.{path}": value for path, value in hits},
            },
        )

    rt = get_runtime()

    # Step 2 — estimate, then budget guard (0 stands in for unknown).
    # reserve() checks the caps AND records the estimate in one synchronous
    # step, so concurrent generate() calls cannot race between check and
    # record and collectively overspend the session budget.
    estimated, source, warning = await resolve_estimate(model_id, params)
    reservation = rt.budget.reserve(estimated or 0, confirm)

    # Step 3 — submit. The client never auto-retries this POST.
    try:
        generation_id = await rt.client.generate(model_id, params)
    except BaseException:
        # Nothing was submitted, so nothing was spent — release the hold.
        rt.budget.release(reservation)
        raise

    # Step 4 — re-key the reserved estimate under the real generation id.
    rt.budget.commit(reservation, generation_id)
    logger.info(
        "generation submitted: model_id=%s generation_id=%s "
        "estimated_credits=%s source=%s",
        model_id,
        generation_id,
        estimated,
        source,
    )

    # Step 5 — fire-and-forget path.
    if not wait:
        result: dict = {
            "generation_id": generation_id,
            "status": "processing",
            "output_urls": [],
            "outputs": {},
            "model_id": model_id,
            "credits_spent": None,
            "remaining_balance": None,
            "estimated_credits": estimated,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": None,
        }
        if warning is not None:
            result["warning"] = warning
        return result

    # Step 6/7 — poll to terminal status (or TIMEOUT_PENDING / GENERATION_FAILED).
    timeout = float(timeout_s if timeout_s is not None else rt.settings.default_timeout_s)
    result = await _poll(generation_id, timeout)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    if warning is not None:
        result["warning"] = warning
    return result


@tool_guard
async def get_generation(generation_id: str) -> dict:
    """Fetch the current status and outputs of one generation (no polling).

    A single ``GET /generations/{id}`` snapshot. Use this to check on a job
    started with ``generate(wait=false)`` or after a TIMEOUT_PENDING; use
    ``wait_for_generation`` instead if you want to block until it finishes.

    Statuses: "processing" -> "succeeded" | "failed". ``remaining_balance``
    is only fetched (best-effort) once the job is terminal; while
    processing it is None, as is ``credits_spent``.

    Args:
        generation_id: Id returned by ``generate``.

    Returns:
        {"generation_id", "status", "output_urls", "outputs", "model_id",
        "credits_spent", "remaining_balance", "elapsed_s", "error"} —
        ``error`` carries the provider reason when status is "failed".
        On failure: {"error": {"code", "message", "details"}} (e.g.
        NOT_FOUND for an unknown id).
    """
    rt = get_runtime()
    started = time.monotonic()
    record = await rt.client.get_generation(generation_id)
    status = record.get("status")
    balance: int | None = None
    if status in (_STATUS_SUCCEEDED, _STATUS_FAILED):
        balance = await _fetch_balance()
    return _job_result(
        record,
        generation_id,
        remaining_balance=balance,
        elapsed_s=round(time.monotonic() - started, 3),
    )


@tool_guard
async def wait_for_generation(
    generation_id: str,
    timeout_s: int | None = None,
) -> dict:
    """Block until a generation reaches a terminal status, or time out.

    Resumes waiting on any in-flight job — most usefully after ``generate``
    returned TIMEOUT_PENDING, or for a job started with ``wait=false``.
    Polls with backoff (2s growing to a 10s cap, jittered) until the job is
    "succeeded" or "failed", or until ``timeout_s`` elapses (default:
    PIXIO_DEFAULT_TIMEOUT_S, 180s). Budget actuals are reconciled from the
    job's real ``creditsCost`` when it completes.

    On timeout the TIMEOUT_PENDING error again carries the
    ``generation_id`` — the job keeps running server-side and this tool can
    be called as many times as needed.

    Args:
        generation_id: Id returned by ``generate``.
        timeout_s: Max seconds to wait; None uses the server default.

    Returns:
        On success the job-result shape: {"generation_id", "status",
        "output_urls", "outputs", "model_id", "credits_spent",
        "remaining_balance", "elapsed_s", "error"}. On failure:
        {"error": {"code", "message", "details"}} with code
        GENERATION_FAILED (details include the provider reason),
        TIMEOUT_PENDING (details include generation_id and a resume hint),
        NOT_FOUND, AUTH, or UPSTREAM_ERROR.
    """
    rt = get_runtime()
    started = time.monotonic()
    timeout = float(timeout_s if timeout_s is not None else rt.settings.default_timeout_s)
    result = await _poll(generation_id, timeout)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result
