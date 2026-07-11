"""Shared pytest fixtures for the pixio-mcp test suite (contract T1).

Provides:

- ``TEST_KEY`` — the sentinel API key that redaction tests scan for.
- ``MockAPI`` — a programmable fake Pixio gateway backed by
  ``httpx.MockTransport``. Routes are matched host-agnostically by HTTP
  method plus path *suffix*, so the same transport serves both the Pixio
  API host and arbitrary hosts (e.g. the CDN download route) — required
  because ``PixioClient.download`` fetches non-Pixio URLs.
- ``settings`` / ``mock_api`` / ``runtime`` fixtures used by every test
  module (T1–T3).

All tests are offline-only: no real network traffic ever happens.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pixio_mcp.budget import BudgetGuard
from pixio_mcp.cache import TTLCache
from pixio_mcp.client import PixioClient
from pixio_mcp.config import Settings
from pixio_mcp.runtime import Runtime, init_runtime, reset_runtime

TEST_KEY = "pxio_live_TESTSECRET123"

FLUX_MODEL_ID = "pixio/flux-1/schnell"
NANO_BANANA_MODEL_ID = "pixio/nano-banana-edit"
KLING_MODEL_ID = "pixio/kling-master"

CDN_OUTPUT_URL = "https://cdn.example/out.png"
MEDIA_UPLOAD_URL = "https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/test.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

RouteHandler = Callable[[httpx.Request], httpx.Response]

DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "id": FLUX_MODEL_ID,
        "providerId": "pixio",
        "name": "FLUX.1 Schnell",
        "description": "Fast, cheap text-to-image generation.",
        "type": "text-to-image",
        "credits": 1,
        "company": "Black Forest Labs",
        "inputs": ["prompt", "image_size"],
    },
    {
        "id": NANO_BANANA_MODEL_ID,
        "providerId": "pixio",
        "name": "Nano Banana Edit",
        "description": "Instruction-driven image editing.",
        "type": "image-to-image",
        "credits": 4,
        "company": "Google",
        "inputs": ["prompt", "image_url"],
    },
    {
        "id": KLING_MODEL_ID,
        "providerId": "pixio",
        "name": "Kling Master",
        "description": "High-fidelity image-to-video generation.",
        "type": "image-to-video",
        "credits": 295,
        "company": "Kuaishou",
        "inputs": ["prompt", "image_url"],
    },
]

FLUX_PARAMS_RESPONSE: dict[str, Any] = {
    "model": {
        "id": FLUX_MODEL_ID,
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
            "placeholder": "Describe the image",
        },
        {
            "name": "image_size",
            "type": "select",
            "label": "Image Size",
            "required": False,
            "defaultValue": "square_hd",
            "options": [
                {"value": "square_hd", "label": "Square HD"},
                {"value": "landscape_4_3", "label": "Landscape 4:3"},
                {"value": "portrait_4_3", "label": "Portrait 4:3"},
            ],
        },
    ],
}

GEN_SUCCEEDED: dict[str, Any] = {
    "id": "gen-123",
    "status": "succeeded",
    "type": "text-to-image",
    "providerId": "pixio",
    "modelId": FLUX_MODEL_ID,
    "params": {"prompt": "test"},
    "outputUrl": CDN_OUTPUT_URL,
    "outputs": {"imageUrl": CDN_OUTPUT_URL},
    "assetVariants": [],
    "error": None,
    "creditsCost": 1,
    "createdAt": "2026-07-11T00:00:00.000Z",
    "updatedAt": "2026-07-11T00:00:05.000Z",
    "billedAt": "2026-07-11T00:00:05.000Z",
}

CREDITS_RESPONSE: dict[str, Any] = {
    "accountId": "acc",
    "total": 1000,
    "recurring": {
        "current": 1000,
        "quota": 15000,
        "lastTopOffAt": "2026-07-01T00:00:00.000Z",
    },
    "permanent": 0,
}

LEDGER_RESPONSE: dict[str, Any] = {
    "entries": [
        {
            "id": "led-1",
            "reason": "generation",
            "deltaRecurring": -1,
            "deltaPermanent": 0,
            "sourceId": "gen-122",
            "createdAt": "2026-07-10T12:00:00.000Z",
        },
        {
            "id": "led-2",
            "reason": "generation",
            "deltaRecurring": -4,
            "deltaPermanent": 0,
            "sourceId": "gen-121",
            "createdAt": "2026-07-10T11:00:00.000Z",
        },
    ],
}


def _static(response: httpx.Response) -> RouteHandler:
    """Freeze a Response into a handler that yields a fresh copy per request.

    Re-serving the same ``httpx.Response`` instance is unsafe (its body
    stream can only be consumed once when read as a stream), so capture the
    status, headers, and content once and rebuild per request.
    """
    status = response.status_code
    content = response.content
    headers = [
        (key, value)
        for key, value in response.headers.multi_items()
        if key.lower() != "content-length"
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status, content=content, headers=headers)

    return handler


def _estimate_route(request: httpx.Request) -> httpx.Response:
    """Default POST /generations/estimate route: 1 credit, echoes modelId."""
    body = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={
            "success": True,
            "modelId": body.get("modelId", FLUX_MODEL_ID),
            "currency": "credits",
            "baseCost": 1,
            "estimatedCost": 1,
        },
    )


def _generate_route(request: httpx.Request) -> httpx.Response:
    """Default POST /generate route: accepts the job as contentId gen-123."""
    body = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={
            "success": True,
            "message": "ok",
            "contentId": "gen-123",
            "providerId": body.get("providerId", "pixio"),
            "modelId": body.get("modelId", FLUX_MODEL_ID),
        },
    )


class MockAPI:
    """Programmable fake Pixio gateway for ``httpx.MockTransport``.

    Routes are matched by upper-cased HTTP method plus the END of the
    request URL path (``path.endswith(suffix)``), host-agnostically, so it
    serves the Pixio API host and arbitrary CDN hosts alike. Later
    ``.on()`` registrations override earlier ones, including the defaults
    installed at construction. Every request seen — matched or not — is
    appended to ``.requests`` in order.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, RouteHandler]] = []
        self._transport = httpx.MockTransport(self._handle)
        self._install_default_routes()

    @property
    def transport(self) -> httpx.MockTransport:
        """Transport to inject via ``PixioClient(settings, transport=...)``."""
        return self._transport

    def on(
        self,
        method: str,
        path_suffix: str,
        response: httpx.Response | RouteHandler,
    ) -> None:
        """Register a route, overriding any earlier route that also matches.

        Args:
            method: HTTP method; compared upper-cased.
            path_suffix: matched against the END of the request URL path,
                e.g. ``"/generations/estimate"``.
            response: a static ``httpx.Response`` (re-served safely on every
                match) or a callable receiving the ``httpx.Request`` and
                returning a ``httpx.Response``.
        """
        handler = _static(response) if isinstance(response, httpx.Response) else response
        self._routes.append((method.upper(), path_suffix, handler))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path
        for route_method, suffix, handler in reversed(self._routes):
            if method == route_method and path.endswith(suffix):
                return handler(request)
        return httpx.Response(
            404, json={"error": f"MockAPI has no route for {method} {path}"}
        )

    def _install_default_routes(self) -> None:
        self.on("GET", "/models", httpx.Response(200, json={"models": DEFAULT_MODELS}))
        self.on("GET", "/params", httpx.Response(200, json=FLUX_PARAMS_RESPONSE))
        self.on("POST", "/generations/estimate", _estimate_route)
        self.on("POST", "/generate", _generate_route)
        self.on("GET", "/generations/gen-123", httpx.Response(200, json=GEN_SUCCEEDED))
        self.on("GET", "/credits", httpx.Response(200, json=CREDITS_RESPONSE))
        self.on("GET", "/credits/ledger", httpx.Response(200, json=LEDGER_RESPONSE))
        self.on("POST", "/media", httpx.Response(200, json={"url": MEDIA_UPLOAD_URL}))
        self.on(
            "GET",
            "/out.png",
            httpx.Response(
                200, content=PNG_BYTES, headers={"content-type": "image/png"}
            ),
        )


@pytest.fixture
def mock_api() -> MockAPI:
    """A fresh programmable fake Pixio gateway with default routes."""
    return MockAPI()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with the sentinel key, default caps (60/300), tmp downloads."""
    return Settings.from_env(
        env={
            "PIXIO_API_KEY": TEST_KEY,
            "PIXIO_DOWNLOAD_DIR": str(tmp_path / "outputs"),
        }
    )


@pytest.fixture
async def runtime(settings: Settings, mock_api: MockAPI) -> AsyncIterator[Runtime]:
    """Initialized global Runtime wired to the mock transport.

    Teardown closes the client best-effort and resets the global runtime.
    """
    client = PixioClient(settings, transport=mock_api.transport)
    rt = Runtime(
        settings=settings,
        client=client,
        budget=BudgetGuard(60, 300),
        catalog_cache=TTLCache(600.0),
    )
    init_runtime(rt)
    try:
        yield rt
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
        reset_runtime()
