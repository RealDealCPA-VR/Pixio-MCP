"""Offline tests for ``tools/credits.py``.

Covers the ``estimate_cost`` happy path, the estimate-endpoint-failure fallback
to catalog credits, the estimate==0 cap-bypass fix (zero is not a usable
estimate — v1.1 addendum #2 three-case table), the double-miss (estimate +
catalog) unknown result with a warning, the shared ``resolve_estimate`` helper
triple, input leniency (params-as-JSON-string, backticked model ids —
addendum #3), context economy of the tool docstrings (addendum #1/#4), and
``get_credits`` including the optional ledger tail with ``ledger_limit``
respected.

All tests run against the ``MockAPI`` transport from conftest — no network.
The 500-estimate tests deliberately exercise the client's 5xx retry path, so
they sleep through the contract backoff (0.5s/1s/2s) in real time.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, get_type_hints

import httpx
from pydantic.fields import FieldInfo

from conftest import MockAPI
from pixio_mcp.runtime import Runtime
from pixio_mcp.tools.credits import (
    PARAMS_TYPE_MESSAGE,
    estimate_cost,
    get_credits,
    resolve_estimate,
)

# Fully-known catalog so the fallback lookup never depends on MockAPI defaults.
CATALOG: dict[str, Any] = {
    "models": [
        {
            "id": "pixio/flux-1/schnell",
            "providerId": "pixio",
            "name": "FLUX.1 Schnell",
            "description": "Fast text-to-image generation.",
            "type": "text-to-image",
            "credits": 1,
            "company": "Black Forest Labs",
            "inputs": [],
        },
        {
            "id": "pixio/kling-video/v2.1/master",
            "providerId": "pixio",
            "name": "Kling 2.1 Master",
            "description": "Premium image-to-video rendering.",
            "type": "image-to-video",
            "credits": 295,
            "company": "Kling",
            "inputs": [],
        },
        {
            "id": "pixio/video-ops/free-op",
            "providerId": "pixio",
            "name": "Free Video Op",
            "description": "A genuinely free video operation.",
            "type": "video-to-video",
            "credits": 0,
            "company": "Pixio",
            "inputs": [],
        },
    ]
}


def _break_estimate(mock_api: MockAPI) -> None:
    """Make POST /generations/estimate fail with a 500 on every attempt."""
    mock_api.on(
        "POST",
        "/generations/estimate",
        lambda _req: httpx.Response(500, json={"error": "internal server error"}),
    )


def _zero_estimate(mock_api: MockAPI) -> None:
    """Make POST /generations/estimate report estimatedCost 0 (unusable)."""
    mock_api.on(
        "POST",
        "/generations/estimate",
        lambda _req: httpx.Response(
            200,
            json={
                "success": True,
                "modelId": "any",
                "currency": "credits",
                "baseCost": 0,
                "estimatedCost": 0,
            },
        ),
    )


def _ledger_requests(mock_api: MockAPI) -> list[httpx.Request]:
    """Every GET /credits/ledger request the mock gateway has seen."""
    return [
        req
        for req in mock_api.requests
        if req.method == "GET" and req.url.path.endswith("/credits/ledger")
    ]


async def test_estimate_cost_happy_path(runtime: Runtime, mock_api: MockAPI) -> None:
    """estimate_cost returns the estimatedCost with source "estimate"."""
    result = await estimate_cost("pixio/flux-1/schnell", {"prompt": "a cat"})

    assert "error" not in result
    assert result["model_id"] == "pixio/flux-1/schnell"
    assert result["estimated_credits"] == 1
    assert result["source"] == "estimate"

    estimate_requests = [
        req
        for req in mock_api.requests
        if req.method == "POST" and req.url.path.endswith("/generations/estimate")
    ]
    assert len(estimate_requests) == 1
    body = json.loads(estimate_requests[0].content)
    assert body["providerId"] == "pixio"
    assert body["modelId"] == "pixio/flux-1/schnell"
    assert body["params"] == {"prompt": "a cat"}


async def test_estimate_500_falls_back_to_catalog_credits(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A 500 from the estimate endpoint falls back to the model's catalog credits."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _break_estimate(mock_api)

    result = await estimate_cost(
        "pixio/kling-video/v2.1/master", {"image_url": "https://cdn.example/in.png"}
    )

    assert "error" not in result
    assert result["model_id"] == "pixio/kling-video/v2.1/master"
    assert result["estimated_credits"] == 295
    assert result["source"] == "catalog"


async def test_estimate_and_catalog_both_miss(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Estimate failing AND the model absent from the catalog yields unknown + warning."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _break_estimate(mock_api)

    result = await estimate_cost("pixio/ghost/unknown-model", {"prompt": "x"})

    assert "error" not in result
    assert result["estimated_credits"] is None
    assert result["source"] == "unknown"
    assert isinstance(result.get("warning"), str)
    assert result["warning"]


async def test_resolve_estimate_happy_triple(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """resolve_estimate returns the same (credits, source, warning) triple."""
    estimated, source, warning = await resolve_estimate(
        "pixio/flux-1/schnell", {"prompt": "a cat"}
    )

    assert estimated == 1
    assert source == "estimate"
    assert warning is None


async def test_resolve_estimate_unknown_triple(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """resolve_estimate mirrors the double-miss result: (None, "unknown", warning)."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _break_estimate(mock_api)

    estimated, source, warning = await resolve_estimate(
        "pixio/ghost/unknown-model", {"prompt": "x"}
    )

    assert estimated is None
    assert source == "unknown"
    assert isinstance(warning, str)
    assert warning


async def test_estimate_zero_falls_back_to_catalog_cost(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #2: estimatedCost 0 is unusable — the catalog cost applies.

    Regression: a 0 estimate used to be accepted verbatim, bypassing the
    budget caps for expensive models the gateway cannot price.
    """
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _zero_estimate(mock_api)

    estimated, source, warning = await resolve_estimate(
        "pixio/kling-video/v2.1/master", {"image_url": "https://cdn.example/in.png"}
    )

    assert estimated == 295
    assert source == "catalog"
    assert isinstance(warning, str) and "0" in warning

    result = await estimate_cost(
        "pixio/kling-video/v2.1/master", {"image_url": "https://cdn.example/in.png"}
    )
    assert "code" not in (result.get("error") or {})
    assert result["estimated_credits"] == 295
    assert result["source"] == "catalog"
    assert isinstance(result.get("warning"), str)


async def test_estimate_zero_catalog_zero_is_genuinely_free(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #2: estimate 0 + catalog 0 -> (0, "catalog", None) — free."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _zero_estimate(mock_api)

    estimated, source, warning = await resolve_estimate(
        "pixio/video-ops/free-op", {"video_url": "https://cdn.example/in.mp4"}
    )

    assert estimated == 0
    assert source == "catalog"
    assert warning is None


async def test_estimate_zero_catalog_unknown_is_unknown_with_warning(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #2: estimate 0 + model absent from catalog -> unknown + warning."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=CATALOG))
    _zero_estimate(mock_api)

    estimated, source, warning = await resolve_estimate(
        "pixio/ghost/unknown-model", {"prompt": "x"}
    )

    assert estimated is None
    assert source == "unknown"
    assert isinstance(warning, str) and warning


async def test_estimate_cost_accepts_params_as_json_string(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #3: a JSON-encoded params string is unwrapped to the object."""
    result = await estimate_cost("pixio/flux-1/schnell", '{"prompt": "hi"}')

    assert not isinstance(result.get("error"), dict)
    assert result["estimated_credits"] == 1
    assert result["source"] == "estimate"

    estimate_requests = [
        req
        for req in mock_api.requests
        if req.method == "POST" and req.url.path.endswith("/generations/estimate")
    ]
    assert len(estimate_requests) == 1
    body = json.loads(estimate_requests[0].content)
    assert body["params"] == {"prompt": "hi"}


async def test_estimate_cost_non_dict_params_string_is_validation(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #3: a double-encoded non-object ('"[1, 2]"') -> VALIDATION."""
    result = await estimate_cost("pixio/flux-1/schnell", '"[1, 2]"')

    err = result.get("error")
    assert isinstance(err, dict)
    assert err["code"] == "VALIDATION"
    assert err["message"] == PARAMS_TYPE_MESSAGE
    assert mock_api.requests == [], "invalid params must fail before any request"


async def test_estimate_cost_strips_backticks_and_whitespace_from_model_id(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Addendum #3: ' `pixio/flux-1/schnell` ' resolves like the clean id."""
    result = await estimate_cost(" `pixio/flux-1/schnell` ", {"prompt": "a cat"})

    assert not isinstance(result.get("error"), dict)
    assert result["model_id"] == "pixio/flux-1/schnell"
    assert result["estimated_credits"] == 1

    estimate_requests = [
        req
        for req in mock_api.requests
        if req.method == "POST" and req.url.path.endswith("/generations/estimate")
    ]
    body = json.loads(estimate_requests[0].content)
    assert body["modelId"] == "pixio/flux-1/schnell"


def test_credit_tool_schemas_have_field_descriptions_and_no_args_sections() -> None:
    """Addendum #1/#4: every param carries a Field description; Args: dropped."""
    for tool in (estimate_cost, get_credits):
        doc = inspect.getdoc(tool) or ""
        assert doc, f"{tool.__name__} must keep a docstring"
        assert "Args:" not in doc, f"{tool.__name__} docstring must drop Args:"
        hints = get_type_hints(tool, include_extras=True)
        assert str(hints["return"]).startswith("dict"), tool.__name__
        for name in inspect.signature(tool).parameters:
            metadata = getattr(hints[name], "__metadata__", ())
            descriptions = [
                meta.description
                for meta in metadata
                if isinstance(meta, FieldInfo) and meta.description
            ]
            assert descriptions, f"{tool.__name__}.{name} needs a Field description"
            assert all(len(d) <= 120 for d in descriptions), (
                f"{tool.__name__}.{name} description over 120 chars"
            )


async def test_get_credits_shape(runtime: Runtime, mock_api: MockAPI) -> None:
    """get_credits returns {total, recurring{...}, permanent} without a ledger tail."""
    result = await get_credits()

    assert "error" not in result
    assert result["total"] == 1000
    assert result["permanent"] == 0
    assert result["recurring"]["current"] == 1000
    assert result["recurring"]["quota"] == 15000
    assert "lastTopOffAt" in result["recurring"]
    assert "ledger_tail" not in result
    assert _ledger_requests(mock_api) == []


async def test_get_credits_ledger_tail_respects_limit(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """include_ledger_tail=True adds ledger_tail, truncated to ledger_limit entries."""
    limited = await get_credits(include_ledger_tail=True, ledger_limit=1)

    assert "error" not in limited
    assert isinstance(limited["ledger_tail"], list)
    assert len(limited["ledger_tail"]) == 1
    assert isinstance(limited["ledger_tail"][0], dict)

    full = await get_credits(include_ledger_tail=True)
    assert len(full["ledger_tail"]) == 2  # MockAPI default ledger has 2 entries
    assert all(isinstance(entry, dict) for entry in full["ledger_tail"])

    assert len(_ledger_requests(mock_api)) == 2
