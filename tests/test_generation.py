"""Offline tests for ``pixio_mcp.tools.generation``.

Covers the generate/wait lifecycle against the MockAPI gateway: happy path
(contract job-result shape), fire-and-forget ``wait=False``, the poll loop,
wait-timeout resume (# AC-4), failed-generation mapping (# AC-8), budget
refusal + confirm override (# AC-3), session-budget trips, and budget
reconciliation of estimated vs actual credits.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

import httpx
import pytest

from pixio_mcp.tools import generation as generation_module
from pixio_mcp.tools.generation import generate, get_generation, wait_for_generation

if TYPE_CHECKING:
    from conftest import MockAPI
    from pixio_mcp.runtime import Runtime

MODEL_ID = "pixio/flux-1/schnell"
OUT_URL = "https://cdn.example/out.png"

JOB_RESULT_KEYS = frozenset(
    {
        "generation_id",
        "status",
        "output_urls",
        "outputs",
        "model_id",
        "credits_spent",
        "remaining_balance",
        "elapsed_s",
        "error",
    }
)


def _gen_body(status: str, **overrides: Any) -> dict[str, Any]:
    """Build a live-shaped ``GET /generations/{id}`` body for the mock."""
    body: dict[str, Any] = {
        "id": "gen-123",
        "status": status,
        "type": "image",
        "providerId": "pixio",
        "modelId": MODEL_ID,
        "params": {"prompt": "a cat"},
        "outputUrl": None,
        "outputs": {},
        "error": None,
        "creditsCost": None,
        "createdAt": "2026-07-11T00:00:00Z",
        "updatedAt": "2026-07-11T00:00:00Z",
    }
    if status == "succeeded":
        body.update(outputUrl=OUT_URL, outputs={"imageUrl": OUT_URL}, creditsCost=1)
    body.update(overrides)
    return body


def _gen_route(status: str, **overrides: Any) -> Callable[[httpx.Request], httpx.Response]:
    """Route callable returning a fresh Response per poll (no stream reuse)."""
    body = _gen_body(status, **overrides)

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return route


def _estimate_route(estimated: int) -> Callable[[httpx.Request], httpx.Response]:
    """Route callable overriding ``POST /generations/estimate``."""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "modelId": MODEL_ID,
                "currency": "credits",
                "baseCost": estimated,
                "estimatedCost": estimated,
            },
        )

    return route


def _error(result: dict[str, Any]) -> dict[str, Any]:
    """Assert ``result`` is an error dict and return its payload.

    Success job results also carry an ``error`` key (provider reason,
    str | None), so an error result is identified by ``error`` being a dict.
    """
    err = result.get("error")
    assert isinstance(err, dict), f"expected an error result, got: {result!r}"
    assert "code" in err and "message" in err
    return err


def _generate_posts(mock_api: MockAPI) -> list[httpx.Request]:
    """Every captured ``POST /generate`` request (spend-side effect)."""
    return [
        r
        for r in mock_api.requests
        if r.method == "POST" and r.url.path.endswith("/generate")
    ]


def _poll_gets(mock_api: MockAPI) -> list[httpx.Request]:
    """Every captured ``GET /generations/{id}`` poll request."""
    return [
        r
        for r in mock_api.requests
        if r.method == "GET" and "/generations/" in r.url.path
    ]


def _install_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap every ``asyncio.sleep`` at 10 ms so poll backoff never stalls tests."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay: float, result: Any = None) -> Any:
        return await real_sleep(min(float(delay), 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    if hasattr(generation_module, "sleep"):
        monkeypatch.setattr(generation_module, "sleep", _fast_sleep)


async def test_generate_happy_path_matches_job_result_shape(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A succeeded generation returns the exact contract job-result shape."""
    _install_fast_sleep(monkeypatch)
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    assert JOB_RESULT_KEYS <= set(result)
    assert result["generation_id"] == "gen-123"
    assert result["status"] == "succeeded"
    assert result["output_urls"] == [OUT_URL]
    assert result["outputs"].get("imageUrl") == OUT_URL
    assert isinstance(result["model_id"], str)
    assert result["credits_spent"] == 1
    assert result["remaining_balance"] == 1000
    assert isinstance(result["elapsed_s"], float) and result["elapsed_s"] >= 0.0
    assert result["error"] is None
    assert len(_generate_posts(mock_api)) == 1


async def test_generate_output_urls_ordered_and_deduped(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """output_urls is outputUrl first, then http(s) outputs values, unique."""
    _install_fast_sleep(monkeypatch)
    thumb = "https://cdn.example/thumb.png"
    mock_api.on(
        "GET",
        "/generations/gen-123",
        _gen_route(
            "succeeded",
            outputs={"imageUrl": OUT_URL, "thumbnailUrl": thumb, "seed": 42},
        ),
    )
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    assert result["status"] == "succeeded"
    assert result["output_urls"] == [OUT_URL, thumb]


async def test_generate_wait_false_returns_processing_without_polling(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """wait=False returns immediately: processing status, estimate, no polls."""
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=False)
    assert not isinstance(result.get("error"), dict)
    assert result["status"] == "processing"
    assert result["generation_id"] == "gen-123"
    assert result["credits_spent"] is None
    assert result["remaining_balance"] is None
    assert result["estimated_credits"] == 1
    assert _poll_gets(mock_api) == [], "wait=False must not poll /generations/{id}"


async def test_generate_polls_until_succeeded(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poll loop rides out transient processing statuses to the terminal one."""
    _install_fast_sleep(monkeypatch)
    seen = {"count": 0}

    def route(request: httpx.Request) -> httpx.Response:
        seen["count"] += 1
        if seen["count"] <= 2:
            return httpx.Response(200, json=_gen_body("processing"))
        return httpx.Response(200, json=_gen_body("succeeded"))

    mock_api.on("GET", "/generations/gen-123", route)
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    assert result["status"] == "succeeded"
    assert result["generation_id"] == "gen-123"
    assert result["credits_spent"] == 1
    assert seen["count"] >= 3, "expected two processing polls before the succeeded one"


async def test_generate_wait_timeout_returns_pending_then_resumes(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC-4: wait timeout -> TIMEOUT_PENDING with the id; wait_for_generation
    # later completes the SAME job once the provider finishes.
    _install_fast_sleep(monkeypatch)
    mock_api.on("GET", "/generations/gen-123", _gen_route("processing"))
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True, timeout_s=1)
    err = _error(result)
    assert err["code"] == "TIMEOUT_PENDING"
    assert err["details"]["generation_id"] == "gen-123"
    assert err["details"]["timeout_s"] == 1
    assert "wait_for_generation" in err["details"]["hint"]

    mock_api.on("GET", "/generations/gen-123", _gen_route("succeeded"))
    resumed = await wait_for_generation("gen-123")
    assert not isinstance(resumed.get("error"), dict)
    assert resumed["generation_id"] == "gen-123"
    assert resumed["status"] == "succeeded"
    assert resumed["credits_spent"] == 1
    assert resumed["remaining_balance"] == 1000
    assert isinstance(resumed["elapsed_s"], float)
    # estimate (1) was recorded at submit; actual creditsCost (1) reconciles
    # with no double count across generate + wait_for_generation.
    assert runtime.budget.session_spent == 1


async def test_generate_failed_surfaces_provider_reason(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC-8: terminal failed status -> GENERATION_FAILED with the provider's
    # reason string from the mock error field.
    _install_fast_sleep(monkeypatch)
    reason = "NSFW content detected by provider"
    mock_api.on(
        "GET",
        "/generations/gen-123",
        _gen_route("failed", error=reason, creditsCost=0),
    )
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    err = _error(result)
    assert err["code"] == "GENERATION_FAILED"
    assert err["details"]["generation_id"] == "gen-123"
    assert err["details"]["provider_reason"] == reason


async def test_generate_over_per_job_cap_refused_then_confirm_proceeds(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC-3: an estimate above PIXIO_MAX_CREDITS_PER_JOB is refused with
    # BUDGET_EXCEEDED naming the estimate, the cap, and the confirm override,
    # spending nothing; the same call with confirm=True proceeds.
    mock_api.on("POST", "/generations/estimate", _estimate_route(100))

    refused = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    err = _error(refused)
    assert err["code"] == "BUDGET_EXCEEDED"
    message = err["message"]
    assert "100" in message, "message must state the estimate"
    assert "60" in message, "message must state the per-job cap value"
    assert "confirm=true" in message.lower(), "message must state the override"
    details = err["details"]
    assert details["estimated_credits"] == 100
    assert details["per_job_cap"] == 60
    assert details["session_budget"] == 300
    assert details["session_spent"] == 0
    assert _generate_posts(mock_api) == [], "refusal must happen before any spend"

    _install_fast_sleep(monkeypatch)
    confirmed = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True, confirm=True)
    assert confirmed["status"] == "succeeded"
    assert confirmed["generation_id"] == "gen-123"
    assert len(_generate_posts(mock_api)) == 1
    # submitted at estimate 100, reconciled down to the actual creditsCost of 1
    assert runtime.budget.session_spent == 1


async def test_generate_session_budget_trip_after_accumulated_spend(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A job under the per-job cap still trips the cumulative session budget."""
    runtime.budget.record_submit("earlier-job", 280)
    mock_api.on("POST", "/generations/estimate", _estimate_route(50))

    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    err = _error(result)
    assert err["code"] == "BUDGET_EXCEEDED"
    assert "300" in err["message"], "message must state the session budget value"
    assert "confirm=true" in err["message"].lower()
    details = err["details"]
    assert details["estimated_credits"] == 50
    assert details["session_spent"] == 280
    assert details["session_budget"] == 300
    assert _generate_posts(mock_api) == []


async def test_budget_reconciliation_estimate_matches_actual_no_drift(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Estimated 1 / creditsCost 1: session spend ends at exactly 1, not 2."""
    _install_fast_sleep(monkeypatch)
    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)
    assert result["credits_spent"] == 1
    assert runtime.budget.session_spent == 1


async def test_concurrent_generates_cannot_overspend_session_budget(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel generate() calls cannot race past the session budget.

    Regression: budget.check and the spend record were separated by the
    awaited POST /generate, so two concurrent calls both passed the check
    against the same stale session_spent and collectively overspent the
    session budget without confirm=true.
    """
    _install_fast_sleep(monkeypatch)
    # 200 already spent; two concurrent 60-credit jobs: the first fits
    # (260 <= 300), the second must be refused (320 > 300).
    runtime.budget.record_submit("earlier-job", 200)
    mock_api.on("POST", "/generations/estimate", _estimate_route(60))

    real_generate = runtime.client.generate

    async def slow_generate(model_id: str, params: dict) -> str:
        await asyncio.sleep(0.02)  # force a suspension like a real round-trip
        return await real_generate(model_id, params)

    monkeypatch.setattr(runtime.client, "generate", slow_generate)

    results = await asyncio.gather(
        generate(MODEL_ID, {"prompt": "a"}, wait=True),
        generate(MODEL_ID, {"prompt": "b"}, wait=True),
    )

    refused = [r for r in results if isinstance(r.get("error"), dict)]
    succeeded = [r for r in results if not isinstance(r.get("error"), dict)]
    assert len(refused) == 1, f"exactly one call must be refused: {results!r}"
    assert refused[0]["error"]["code"] == "BUDGET_EXCEEDED"
    assert len(succeeded) == 1
    assert len(_generate_posts(mock_api)) == 1, "the refused call must not submit"
    # 200 earlier + the one job (estimate 60 reconciled to actual 1) = 201.
    assert runtime.budget.session_spent == 201
    assert runtime.budget.session_spent <= 300


async def test_generate_submit_failure_releases_budget_reservation(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A failed POST /generate returns the reserved estimate to the budget."""
    mock_api.on("POST", "/generations/estimate", _estimate_route(50))
    mock_api.on(
        "POST", "/generate", httpx.Response(400, json={"error": "bad params"})
    )

    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)

    assert _error(result)["code"] == "VALIDATION"
    assert runtime.budget.session_spent == 0, "no submit -> no spend recorded"


async def test_poll_phase_error_result_still_carries_generation_id(
    runtime: Runtime, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upstream/concurrency error during the wait phase keeps the id.

    Regression: credits are committed once POST /generate succeeds and Pixio
    has no list-generations endpoint, so a poll-phase PixioError that lost
    the generation_id made the job permanently unreachable.
    """
    _install_fast_sleep(monkeypatch)
    mock_api.on(
        "GET",
        "/generations/gen-123",
        httpx.Response(
            429,
            json={"error": "This account has reached its API concurrency limit of 3"},
        ),
    )

    result = await generate(MODEL_ID, {"prompt": "a cat"}, wait=True)

    err = _error(result)
    assert err["code"] == "CONCURRENCY"
    assert err["details"]["generation_id"] == "gen-123"
    assert "wait_for_generation" in err["details"]["hint"]
    # the estimate stays recorded — the job was submitted and may bill.
    assert runtime.budget.session_spent == 1


async def test_get_generation_returns_job_shape_from_single_get(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """get_generation does one GET and returns the contract job-result shape."""
    result = await get_generation("gen-123")
    assert JOB_RESULT_KEYS <= set(result)
    assert result["generation_id"] == "gen-123"
    assert result["status"] == "succeeded"
    assert result["output_urls"] == [OUT_URL]
    assert result["credits_spent"] == 1
    assert result["remaining_balance"] == 1000
    assert isinstance(result["elapsed_s"], float)
    assert len(_poll_gets(mock_api)) == 1
