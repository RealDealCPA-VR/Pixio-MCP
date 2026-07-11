"""Offline tests for ``tools/media.py``.

Covers ``upload_media`` (remote-URL JSON mirror vs local-file multipart, plus
VALIDATION for missing paths and directories) and ``download_output`` (files
written with the pinned naming scheme and PNG magic bytes, VALIDATION on a
still-processing generation pointing at wait_for_generation, GENERATION_FAILED
on a failed generation, and dest_dir defaulting to settings.download_dir).

All tests run against the ``MockAPI`` transport from conftest — no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import MockAPI
from pixio_mcp.config import Settings
from pixio_mcp.runtime import Runtime
from pixio_mcp.tools.media import download_output, upload_media

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UPLOADED_URL = "https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/test.png"


def _media_requests(mock_api: MockAPI) -> list[httpx.Request]:
    """Every POST /media request the mock gateway has seen."""
    return [
        req
        for req in mock_api.requests
        if req.method == "POST" and req.url.path.endswith("/media")
    ]


def _cdn_requests(mock_api: MockAPI) -> list[httpx.Request]:
    """Every request that reached the fake output CDN."""
    return [req for req in mock_api.requests if req.url.host == "cdn.example"]


def _generation_body(status: str, *, error: str | None = None) -> dict[str, Any]:
    """A live-shaped GET /generations/{id} body for gen-123 in the given status."""
    succeeded = status == "succeeded"
    return {
        "id": "gen-123",
        "status": status,
        "type": "image",
        "providerId": "pixio",
        "modelId": "pixio/flux-1/schnell",
        "params": {"prompt": "a cat"},
        "outputUrl": "https://cdn.example/out.png" if succeeded else None,
        "outputs": {"imageUrl": "https://cdn.example/out.png"} if succeeded else {},
        "assetVariants": [],
        "error": error,
        "creditsCost": 1 if succeeded else None,
        "createdAt": "2026-07-11T00:00:00Z",
        "updatedAt": "2026-07-11T00:00:05Z",
        "billedAt": None,
    }


def _set_generation_status(
    mock_api: MockAPI, status: str, *, error: str | None = None
) -> None:
    """Override GET /generations/gen-123 to report the given status."""
    body = _generation_body(status, error=error)
    mock_api.on(
        "GET", "/generations/gen-123", lambda _req: httpx.Response(200, json=body)
    )


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/assets/cat.png",
        "http://example.com/assets/dog.jpg",
    ],
)
async def test_upload_media_remote_url_mirrors_via_json(
    runtime: Runtime, mock_api: MockAPI, source: str
) -> None:
    """An http(s) source POSTs JSON {"url": source} to /media as a remote_url."""
    result = await upload_media(source)

    assert "error" not in result
    assert result["source_kind"] == "remote_url"
    assert result["url"] == UPLOADED_URL
    assert "file_name" in result

    media_requests = _media_requests(mock_api)
    assert len(media_requests) == 1
    request = media_requests[0]
    assert request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == {"url": source}


async def test_upload_media_local_file_posts_multipart(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """A local PNG is sent as multipart/form-data and reported as a local_file."""
    # AC-6: local PNG in → permanent pixiomedia.nyc3.digitaloceanspaces.com URL out.
    payload = PNG_MAGIC + b"\x00" * 100
    png_path = tmp_path / "img.png"
    png_path.write_bytes(payload)

    result = await upload_media(str(png_path))

    assert "error" not in result
    assert result["source_kind"] == "local_file"
    assert result["file_name"] == "img.png"
    assert result["size_bytes"] == len(payload)
    assert result["url"] == UPLOADED_URL

    media_requests = _media_requests(mock_api)
    assert len(media_requests) == 1
    content_type = media_requests[0].headers["content-type"]
    assert content_type.startswith("multipart/form-data")


async def test_upload_media_missing_path_is_validation(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """A non-existent local path is rejected with VALIDATION before any upload."""
    result = await upload_media(str(tmp_path / "does-not-exist.png"))

    assert result["error"]["code"] == "VALIDATION"
    assert _media_requests(mock_api) == []


async def test_upload_media_directory_is_validation(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """A directory path is rejected with VALIDATION before any upload."""
    result = await upload_media(str(tmp_path))

    assert result["error"]["code"] == "VALIDATION"
    assert _media_requests(mock_api) == []


async def test_download_output_writes_files_with_pinned_names(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """A succeeded generation's outputs land on disk as {id[:8]}-{index}{ext}."""
    dest = tmp_path / "dl"

    result = await download_output("gen-123", dest_dir=str(dest))

    assert "error" not in result
    assert {"generation_id", "files", "dest_dir"} <= result.keys()
    assert result["generation_id"] == "gen-123"
    assert Path(result["dest_dir"]).resolve() == dest.resolve()

    # outputUrl and outputs.imageUrl are the same URL — deduped to one file.
    assert len(result["files"]) == 1
    written = Path(result["files"][0])
    assert written.is_absolute()
    assert written.is_file()
    assert written.parent.resolve() == dest.resolve()
    assert written.name == "gen-123-0.png"  # "gen-123"[:8] + "-0" + ".png"
    assert written.read_bytes()[:8] == PNG_MAGIC


async def test_download_output_processing_is_validation(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A still-processing generation is refused, pointing at wait_for_generation."""
    _set_generation_status(mock_api, "processing")

    result = await download_output("gen-123")

    assert result["error"]["code"] == "VALIDATION"
    message = result["error"]["message"]
    assert "processing" in message.lower()
    assert "wait_for_generation" in message
    assert _cdn_requests(mock_api) == []


async def test_download_output_failed_is_generation_failed(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A failed generation surfaces GENERATION_FAILED instead of downloading."""
    _set_generation_status(mock_api, "failed", error="NSFW content detected")

    result = await download_output("gen-123")

    assert result["error"]["code"] == "GENERATION_FAILED"
    assert _cdn_requests(mock_api) == []


async def test_download_output_defaults_to_settings_download_dir(
    runtime: Runtime, settings: Settings, mock_api: MockAPI
) -> None:
    """With no dest_dir, files are written under settings.download_dir (created)."""
    result = await download_output("gen-123")

    assert "error" not in result
    assert Path(result["dest_dir"]).resolve() == settings.download_dir.resolve()
    assert settings.download_dir.is_dir()
    assert len(result["files"]) == 1
    written = Path(result["files"][0])
    assert written.parent.resolve() == settings.download_dir.resolve()
    assert written.read_bytes()[:8] == PNG_MAGIC
