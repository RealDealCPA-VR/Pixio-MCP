"""Unit tests for pixio_mcp.client.PixioClient (contract B2).

Offline-only: every request is served by the MockAPI transport from
conftest, including "CDN" downloads to non-Pixio hosts. Retry backoff
sleeps are patched out so retry tests run instantly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import pixio_mcp.client as pixio_client_module
from conftest import (
    CDN_OUTPUT_URL,
    DEFAULT_MODELS,
    FLUX_MODEL_ID,
    PNG_BYTES,
    TEST_KEY,
    MockAPI,
)
from pixio_mcp.client import PixioClient
from pixio_mcp.config import Settings
from pixio_mcp.errors import ErrorCode, PixioError


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace backoff sleeps with instant no-ops; returns recorded delays."""
    delays: list[float] = []

    async def _instant(delay: float, *args: object, **kwargs: object) -> None:
        delays.append(float(delay))

    monkeypatch.setattr(asyncio, "sleep", _instant)
    if hasattr(pixio_client_module, "sleep"):
        monkeypatch.setattr(pixio_client_module, "sleep", _instant)
    try:
        import anyio
    except ImportError:
        pass
    else:
        monkeypatch.setattr(anyio, "sleep", _instant)
    return delays


@pytest.fixture
async def client(settings: Settings, mock_api: MockAPI) -> AsyncIterator[PixioClient]:
    """A PixioClient wired to the MockAPI transport, closed on teardown."""
    pixio_client = PixioClient(settings, transport=mock_api.transport)
    try:
        yield pixio_client
    finally:
        await pixio_client.aclose()


async def test_get_models_unwraps_list_and_sends_bearer(
    client: PixioClient, mock_api: MockAPI
) -> None:
    models = await client.get_models()
    assert isinstance(models, list)
    assert [m["id"] for m in models] == [m["id"] for m in DEFAULT_MODELS]
    request = mock_api.requests[0]
    assert request.url.path.endswith("/models")
    assert request.headers["authorization"] == f"Bearer {TEST_KEY}"


async def test_get_5xx_retried_three_times_then_upstream_error(
    client: PixioClient, mock_api: MockAPI, fast_sleep: list[float]
) -> None:
    mock_api.on("GET", "/models", httpx.Response(500, json={"error": "internal"}))
    with pytest.raises(PixioError) as excinfo:
        await client.get_models()
    assert excinfo.value.code == ErrorCode.UPSTREAM_ERROR
    attempts = [r for r in mock_api.requests if r.url.path.endswith("/models")]
    assert len(attempts) == 4  # initial attempt + 3 retries (0.5s/1s/2s backoff)


async def test_get_transport_error_retried_until_success(
    client: PixioClient, mock_api: MockAPI, fast_sleep: list[float]
) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"models": DEFAULT_MODELS})

    mock_api.on("GET", "/models", flaky)
    models = await client.get_models()
    assert len(models) == len(DEFAULT_MODELS)
    assert len(calls) == 3  # two transport failures, then the retried success


async def test_post_generate_5xx_makes_exactly_one_request(
    client: PixioClient, mock_api: MockAPI, fast_sleep: list[float]
) -> None:
    mock_api.on("POST", "/generate", httpx.Response(502, json={"error": "bad gateway"}))
    with pytest.raises(PixioError) as excinfo:
        await client.generate(FLUX_MODEL_ID, {"prompt": "x"})
    assert excinfo.value.code == ErrorCode.UPSTREAM_ERROR
    posts = [
        r
        for r in mock_api.requests
        if r.method == "POST" and r.url.path.endswith("/generate")
    ]
    assert len(posts) == 1  # spend safety: POST /generate is NEVER retried


async def test_generate_returns_content_id_and_posts_provider_id(
    client: PixioClient, mock_api: MockAPI
) -> None:
    content_id = await client.generate(FLUX_MODEL_ID, {"prompt": "a red fox"})
    assert content_id == "gen-123"
    request = next(
        r
        for r in mock_api.requests
        if r.method == "POST" and r.url.path.endswith("/generate")
    )
    body = json.loads(request.content)
    assert body["providerId"] == "pixio"
    assert body["modelId"] == FLUX_MODEL_ID
    assert body["params"] == {"prompt": "a red fox"}


async def test_generate_missing_content_id_in_2xx_is_upstream_error(
    client: PixioClient, mock_api: MockAPI
) -> None:
    mock_api.on("POST", "/generate", httpx.Response(200, json={"success": True, "message": "ok"}))
    with pytest.raises(PixioError) as excinfo:
        await client.generate(FLUX_MODEL_ID, {"prompt": "x"})
    assert excinfo.value.code == ErrorCode.UPSTREAM_ERROR


async def test_401_maps_to_auth(client: PixioClient, mock_api: MockAPI) -> None:
    mock_api.on("GET", "/credits", httpx.Response(401, json={"error": "Invalid API key"}))
    with pytest.raises(PixioError) as excinfo:
        await client.get_credits()
    assert excinfo.value.code == ErrorCode.AUTH


async def test_402_maps_to_insufficient_credits_with_details(
    client: PixioClient, mock_api: MockAPI
) -> None:
    body = {
        "error": "Insufficient credits",
        "availableCredits": 3,
        "requiredCredits": 10,
        "shortfall": 7,
    }
    mock_api.on("POST", "/generate", httpx.Response(402, json=body))
    with pytest.raises(PixioError) as excinfo:
        await client.generate(FLUX_MODEL_ID, {"prompt": "x"})
    err = excinfo.value
    assert err.code == ErrorCode.INSUFFICIENT_CREDITS
    details = err.to_dict()["error"]["details"]
    assert details["availableCredits"] == 3
    assert details["requiredCredits"] == 10
    assert details["shortfall"] == 7


async def test_404_maps_to_not_found(client: PixioClient, mock_api: MockAPI) -> None:
    mock_api.on("GET", "/params", httpx.Response(404, json={"error": "Pixio API model not found"}))
    with pytest.raises(PixioError) as excinfo:
        await client.get_params("pixio/does-not-exist")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


async def test_429_concurrency_maps_with_limit_in_details(
    client: PixioClient, mock_api: MockAPI
) -> None:
    body = {
        "error": "This account has reached its API concurrency limit of 3 concurrent generations",
        "generationId": "gen-busy",
        "status": "processing",
        "concurrencyLimit": 3,
    }
    mock_api.on("POST", "/generate", httpx.Response(429, json=body))
    with pytest.raises(PixioError) as excinfo:
        await client.generate(FLUX_MODEL_ID, {"prompt": "x"})
    err = excinfo.value
    assert err.code == ErrorCode.CONCURRENCY
    assert err.to_dict()["error"]["details"]["concurrencyLimit"] == 3


async def test_400_hidden_body_text_surfaced_in_validation_message(
    client: PixioClient, mock_api: MockAPI
) -> None:
    mock_api.on(
        "POST",
        "/generations/estimate",
        httpx.Response(400, json={"error": "Missing required parameter: image_size"}),
    )
    with pytest.raises(PixioError) as excinfo:
        await client.estimate(FLUX_MODEL_ID, {"prompt": "x"})
    err = excinfo.value
    assert err.code == ErrorCode.VALIDATION
    assert "Missing required parameter: image_size" in err.to_dict()["error"]["message"]


async def test_empty_api_key_raises_auth_with_zero_requests(
    mock_api: MockAPI, tmp_path: Path
) -> None:
    empty_key_settings = Settings.from_env(
        env={"PIXIO_DOWNLOAD_DIR": str(tmp_path / "outputs")}
    )
    assert empty_key_settings.api_key == ""
    pixio_client = PixioClient(empty_key_settings, transport=mock_api.transport)
    try:
        with pytest.raises(PixioError) as excinfo:
            await pixio_client.get_models()
        err = excinfo.value
        assert err.code == ErrorCode.AUTH
        assert "PIXIO_API_KEY" in err.to_dict()["error"]["message"]
        assert mock_api.requests == []  # raised BEFORE any request was sent
    finally:
        await pixio_client.aclose()


async def test_download_writes_bytes_and_omits_auth_for_non_pixio_host(
    client: PixioClient, mock_api: MockAPI, tmp_path: Path
) -> None:
    dest = tmp_path / "out.png"
    written = await client.download(CDN_OUTPUT_URL, dest)
    data = dest.read_bytes()
    assert data == PNG_BYTES
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert written == len(PNG_BYTES)
    cdn_requests = [r for r in mock_api.requests if r.url.host == "cdn.example"]
    assert cdn_requests, "download never hit the CDN host"
    for request in cdn_requests:
        assert "authorization" not in request.headers


async def test_download_sends_auth_to_pixio_host(
    client: PixioClient, mock_api: MockAPI, settings: Settings, tmp_path: Path
) -> None:
    mock_api.on(
        "GET",
        "/hosted/asset.png",
        httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"}),
    )
    pixio_host = httpx.URL(settings.base_url).host
    dest = tmp_path / "asset.png"
    written = await client.download(f"https://{pixio_host}/hosted/asset.png", dest)
    assert written == len(PNG_BYTES)
    assert dest.read_bytes() == PNG_BYTES
    request = next(
        r for r in mock_api.requests if r.url.path.endswith("/hosted/asset.png")
    )
    assert request.headers.get("authorization") == f"Bearer {TEST_KEY}"
