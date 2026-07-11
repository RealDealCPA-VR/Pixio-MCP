"""End-to-end acceptance tests against the mock Pixio gateway.

Covers # AC-2 (four-call discovery-to-disk flow), # AC-5 (local path in
generate params rejected before any spend), and # AC-6 (upload_media returns
the permanent pixiomedia URL).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pixio_mcp.tools import generation as generation_module
from pixio_mcp.tools.catalog import get_model_params, list_models
from pixio_mcp.tools.generation import generate
from pixio_mcp.tools.media import download_output, upload_media

if TYPE_CHECKING:
    from conftest import MockAPI
    from pixio_mcp.config import Settings
    from pixio_mcp.runtime import Runtime

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _install_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap every ``asyncio.sleep`` at 10 ms so poll backoff never stalls tests."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay: float, result: Any = None) -> Any:
        return await real_sleep(min(float(delay), 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    if hasattr(generation_module, "sleep"):
        monkeypatch.setattr(generation_module, "sleep", _fast_sleep)


async def test_local_path_in_params_rejected_before_any_spend(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    # AC-5: a local path inside generate params -> VALIDATION naming the
    # offending field and pointing at upload_media, with zero POST /generate
    # requests ever leaving the client.
    result = await generate(
        "pixio/flux-1/schnell",
        {"prompt": "x", "image_url": "C:\\photos\\in.png"},
        wait=True,
    )
    err = result.get("error")
    assert isinstance(err, dict), f"expected an error result, got: {result!r}"
    assert err["code"] == "VALIDATION"
    assert "params.image_url" in err["message"]
    assert "upload_media" in err["message"]
    assert "image_url" in json.dumps(err.get("details") or {})
    generate_posts = [
        r
        for r in mock_api.requests
        if r.method == "POST" and r.url.path.endswith("/generate")
    ]
    assert generate_posts == [], "no credits may be spent on a rejected call"


async def test_four_call_flow_discovery_to_png_on_disk(
    runtime: Runtime,
    mock_api: MockAPI,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC-2: list_models -> get_model_params -> generate -> download_output
    # yields a real PNG on disk (magic bytes) using only the mock gateway.
    _install_fast_sleep(monkeypatch)

    # Call 1: discovery
    listed = await list_models(type="text-to-image", query="flux")
    assert listed["models"], "catalog should match the flux text-to-image model"
    model = listed["models"][0]
    assert model["type"] == "text-to-image"
    model_id = model["id"]

    # Call 2: input schema
    schema = await get_model_params(model_id)
    assert isinstance(schema.get("params"), list) and schema["params"]

    # Call 3: generation
    result = await generate(model_id, {"prompt": "a red square"}, wait=True)
    assert not isinstance(result.get("error"), dict), f"generate failed: {result!r}"
    assert result["status"] == "succeeded"
    assert result["generation_id"] == "gen-123"
    assert result["credits_spent"] == 1
    assert result["remaining_balance"] == 1000

    # Call 4: download to disk
    saved = await download_output(result["generation_id"])
    assert saved["generation_id"] == "gen-123"
    assert len(saved["files"]) == 1
    file = Path(saved["files"][0])
    assert file.is_absolute()
    assert file.is_file()
    assert file.suffix == ".png"
    assert file.name.startswith("gen-123")
    assert file.read_bytes()[:8] == PNG_MAGIC
    assert Path(saved["dest_dir"]) == settings.download_dir


async def test_upload_media_returns_permanent_pixio_url(
    runtime: Runtime, tmp_path: Path
) -> None:
    # AC-6: a local PNG uploads to the permanent public pixiomedia URL,
    # directly usable as an image_url generation param.
    source = tmp_path / "logo.png"
    data = PNG_MAGIC + b"\x00" * 32
    source.write_bytes(data)

    result = await upload_media(str(source))
    assert result["url"] == "https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/test.png"
    assert result["source_kind"] == "local_file"
    assert result["file_name"] == "logo.png"
    assert result["size_bytes"] == len(data)
