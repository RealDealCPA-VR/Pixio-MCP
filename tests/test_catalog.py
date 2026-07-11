"""Offline tests for ``tools/catalog.py``.

Covers the ``list_models`` contract shape, exact type filtering, case-insensitive
query matching across id/name/description, limit clamping (1..200), offset
paging, description truncation, catalog caching (single upstream GET /models
for repeated calls, expiry via an injectable clock), and ``get_model_params``
verbatim passthrough plus the unknown-model NOT_FOUND mapping.

All tests run against the ``MockAPI`` transport from conftest — no network.
"""

from __future__ import annotations

from typing import Any

import httpx

from conftest import MockAPI
from pixio_mcp.budget import BudgetGuard
from pixio_mcp.cache import TTLCache
from pixio_mcp.client import PixioClient
from pixio_mcp.config import Settings
from pixio_mcp.runtime import Runtime, init_runtime, reset_runtime
from pixio_mcp.tools.catalog import get_model_params, list_models

# A fully-known catalog so filter/paging assertions never depend on the
# MockAPI defaults. Distinctive tokens per field:
#   - "flux-1" appears only in the flux model's id (not its name/description),
#   - "banana edit" appears only in the nano model's *name*,
#   - "sparkle" appears only in the flux model's *description*.
SMALL_CATALOG: dict[str, Any] = {
    "models": [
        {
            "id": "pixio/flux-1/schnell",
            "providerId": "pixio",
            "name": "FLUX.1 Schnell",
            "description": "Fast text-to-image generation with SPARKLE quality.",
            "type": "text-to-image",
            "credits": 1,
            "company": "Black Forest Labs",
            "inputs": [],
        },
        {
            "id": "pixio/nano-banana/edit",
            "providerId": "pixio",
            "name": "Nano Banana Edit",
            "description": "Instruction-driven image modification.",
            "type": "image-to-image",
            "credits": 4,
            "company": "Google",
            "inputs": [],
        },
        {
            "id": "pixio/kling-video/v2.1/master",
            "providerId": "pixio",
            "name": "Kling 2.1 Master",
            "description": "High fidelity image-to-video rendering.",
            "type": "image-to-video",
            "credits": 295,
            "company": "Kling",
            "inputs": [],
        },
    ]
}

PARAMS_BODY: dict[str, Any] = {
    "model": {
        "id": "pixio/flux-1/schnell",
        "name": "FLUX.1 Schnell",
        "type": "text-to-image",
        "credits": 1,
    },
    "params": [
        {
            "name": "prompt",
            "type": "string",
            "label": "Prompt",
            "required": True,
            "defaultValue": "",
        },
        {
            "name": "image_size",
            "type": "select",
            "label": "Image Size",
            "required": False,
            "defaultValue": "landscape_4_3",
            "options": [
                {"value": "landscape_4_3", "label": "Landscape 4:3"},
                {"value": "square_hd", "label": "Square HD"},
            ],
        },
    ],
}


class FakeClock:
    """Mutable monotonic clock injected into TTLCache to drive expiry."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _install_catalog(mock_api: MockAPI, catalog: dict[str, Any]) -> None:
    """Override GET /models with a fully-known catalog (fresh response per hit)."""
    mock_api.on("GET", "/models", lambda _req: httpx.Response(200, json=catalog))


def _big_catalog(count: int = 250) -> dict[str, Any]:
    """A catalog large enough (>200 entries) to observe the upper limit clamp."""
    return {
        "models": [
            {
                "id": f"pixio/test/model-{i:03d}",
                "providerId": "pixio",
                "name": f"Test Model {i:03d}",
                "description": f"Synthetic catalog entry {i:03d}.",
                "type": "text-to-image",
                "credits": 1,
                "company": "TestCo",
                "inputs": [],
            }
            for i in range(count)
        ]
    }


def _models_requests(mock_api: MockAPI) -> list[httpx.Request]:
    """Every GET /models request the mock gateway has seen."""
    return [
        req
        for req in mock_api.requests
        if req.method == "GET" and req.url.path.endswith("/models")
    ]


async def test_list_models_contract_shape(runtime: Runtime, mock_api: MockAPI) -> None:
    """list_models returns the pinned result shape with per-model fields only."""
    result = await list_models()

    assert "error" not in result
    assert {"models", "total_matching", "returned", "offset"} <= result.keys()
    assert isinstance(result["models"], list)
    assert result["models"], "default MockAPI catalog must yield at least one model"
    assert result["returned"] == len(result["models"])
    assert result["total_matching"] >= result["returned"]
    assert result["offset"] == 0
    for model in result["models"]:
        assert set(model.keys()) == {
            "id",
            "name",
            "type",
            "credits",
            "company",
            "description",
        }


async def test_type_filter_exact_match(runtime: Runtime, mock_api: MockAPI) -> None:
    """`type` is an exact match on the model type — never a substring match."""
    _install_catalog(mock_api, SMALL_CATALOG)

    result = await list_models(type="text-to-image")
    assert "error" not in result
    assert result["total_matching"] == 1
    assert result["returned"] == 1
    assert result["models"][0] == {
        "id": "pixio/flux-1/schnell",
        "name": "FLUX.1 Schnell",
        "type": "text-to-image",
        "credits": 1,
        "company": "Black Forest Labs",
        "description": "Fast text-to-image generation with SPARKLE quality.",
    }

    # "image" is a substring of every type in the catalog but an exact match of none.
    none_matched = await list_models(type="image")
    assert none_matched["total_matching"] == 0
    assert none_matched["returned"] == 0
    assert none_matched["models"] == []


async def test_query_case_insensitive_across_id_name_description(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """`query` matches case-insensitively over id, name, and description."""
    _install_catalog(mock_api, SMALL_CATALOG)

    # Matches via id only ("flux-1" is not in the name "FLUX.1 Schnell").
    by_id = await list_models(query="FLUX-1")
    assert by_id["total_matching"] == 1
    assert by_id["models"][0]["id"] == "pixio/flux-1/schnell"

    # Matches via name only (id has "banana/edit", not "banana edit").
    by_name = await list_models(query="bAnAnA eDiT")
    assert by_name["total_matching"] == 1
    assert by_name["models"][0]["id"] == "pixio/nano-banana/edit"

    # Matches via description only.
    by_description = await list_models(query="sparkle")
    assert by_description["total_matching"] == 1
    assert by_description["models"][0]["id"] == "pixio/flux-1/schnell"

    # No hit anywhere.
    no_hit = await list_models(query="zebra-hologram")
    assert no_hit["total_matching"] == 0
    assert no_hit["models"] == []


async def test_limit_clamped_to_1_and_200(runtime: Runtime, mock_api: MockAPI) -> None:
    """limit=0 clamps up to 1; limit=999 clamps down to 200."""
    _install_catalog(mock_api, _big_catalog(250))

    low = await list_models(limit=0)
    assert "error" not in low
    assert low["returned"] == 1
    assert len(low["models"]) == 1
    assert low["total_matching"] == 250

    high = await list_models(limit=999)
    assert "error" not in high
    assert high["returned"] == 200
    assert len(high["models"]) == 200
    assert high["total_matching"] == 250


async def test_offset_paging(runtime: Runtime, mock_api: MockAPI) -> None:
    """offset skips catalog-order entries; paging past the end returns nothing."""
    _install_catalog(mock_api, SMALL_CATALOG)

    page_two = await list_models(limit=1, offset=1)
    assert page_two["models"][0]["id"] == "pixio/nano-banana/edit"
    assert page_two["returned"] == 1
    assert page_two["total_matching"] == 3
    assert page_two["offset"] == 1

    past_end = await list_models(limit=50, offset=10)
    assert past_end["models"] == []
    assert past_end["returned"] == 0
    assert past_end["total_matching"] == 3
    assert past_end["offset"] == 10


async def test_total_matching_vs_returned(runtime: Runtime, mock_api: MockAPI) -> None:
    """total_matching counts all filter hits; returned counts only this page."""
    _install_catalog(mock_api, SMALL_CATALOG)

    result = await list_models(limit=2)
    assert result["total_matching"] == 3
    assert result["returned"] == 2
    assert len(result["models"]) == 2


async def test_description_truncated_to_200_chars(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """Descriptions longer than 200 chars are cut to exactly the first 200."""
    long_description = "abcdefghij" * 50  # 500 chars
    catalog = {
        "models": [
            {
                "id": "pixio/verbose/model",
                "providerId": "pixio",
                "name": "Verbose Model",
                "description": long_description,
                "type": "text-to-image",
                "credits": 2,
                "company": "TestCo",
                "inputs": [],
            }
        ]
    }
    _install_catalog(mock_api, catalog)

    result = await list_models()
    description = result["models"][0]["description"]
    assert len(description) == 200
    assert description == long_description[:200]


async def test_two_calls_hit_upstream_once(runtime: Runtime, mock_api: MockAPI) -> None:
    """The catalog is cached: two list_models calls make exactly one GET /models."""
    first = await list_models()
    second = await list_models(type="text-to-image")

    assert "error" not in first
    assert "error" not in second
    assert len(_models_requests(mock_api)) == 1


async def test_expired_cache_refetches(settings: Settings, mock_api: MockAPI) -> None:
    """After the 600s TTL elapses (fake clock), list_models refetches the catalog."""
    clock = FakeClock(start=1_000.0)
    client = PixioClient(settings, transport=mock_api.transport)
    custom_runtime = Runtime(
        settings=settings,
        client=client,
        budget=BudgetGuard(60, 300),
        catalog_cache=TTLCache(ttl_s=600.0, clock=clock),
    )
    init_runtime(custom_runtime)
    try:
        await list_models()
        clock.now += 100.0  # still inside the TTL — cache must serve this
        await list_models()
        assert len(_models_requests(mock_api)) == 1

        clock.now += 600.0  # 700s since the fetch — entry expired
        refreshed = await list_models()
        assert "error" not in refreshed
        assert len(_models_requests(mock_api)) == 2
    finally:
        reset_runtime()
        await client.aclose()


async def test_get_model_params_verbatim_passthrough(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """get_model_params returns the /params body verbatim and sends modelId."""
    mock_api.on("GET", "/params", lambda _req: httpx.Response(200, json=PARAMS_BODY))

    result = await get_model_params("pixio/flux-1/schnell")
    assert result == PARAMS_BODY

    params_requests = [
        req for req in mock_api.requests if req.url.path.endswith("/params")
    ]
    assert len(params_requests) == 1
    assert params_requests[0].url.params["modelId"] == "pixio/flux-1/schnell"


async def test_get_model_params_unknown_model_maps_not_found(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A 404 for an unknown model surfaces as a NOT_FOUND error dict."""
    mock_api.on(
        "GET",
        "/params",
        lambda _req: httpx.Response(404, json={"error": "Pixio API model not found"}),
    )

    result = await get_model_params("pixio/does-not/exist")
    assert result["error"]["code"] == "NOT_FOUND"
    assert isinstance(result["error"]["message"], str)
    assert result["error"]["message"]
