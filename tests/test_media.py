"""Offline tests for ``tools/media.py``.

Covers ``upload_media`` (remote-URL JSON mirror vs local-file multipart, plus
VALIDATION for missing paths, directories, and data: URLs) and
``download_output`` (files written with the pinned naming scheme and PNG magic
bytes, VALIDATION on any non-succeeded status — processing points at
wait_for_generation, failed carries the provider reason — traversal-safe
filenames, dest_dir defaulting to settings.download_dir, VALIDATION for an
un-creatable dest_dir, generation_id whitespace/backtick stripping, and the
v1.1 requirement that every tool parameter carries a Field description).

All tests run against the ``MockAPI`` transport from conftest — no network.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic.fields import FieldInfo

from conftest import MockAPI
from pixio_mcp.config import Settings
from pixio_mcp.runtime import Runtime
from pixio_mcp.tools.media import download_output, upload_media

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UPLOADED_URL = "https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/test.png"


def _field_descriptions(func: Any) -> dict[str, str | None]:
    """Map each parameter of a tool to its pydantic Field description (or None).

    ``inspect.signature`` follows ``__wrapped__`` through the ``tool_guard``
    decorator, and ``eval_str=True`` resolves the PEP-563 string annotations in
    the tool module's globals — exactly how FastMCP builds the input schema.
    """
    sig = inspect.signature(func, eval_str=True)
    descriptions: dict[str, str | None] = {}
    for name, param in sig.parameters.items():
        description: str | None = None
        for meta in getattr(param.annotation, "__metadata__", ()):
            if isinstance(meta, FieldInfo) and meta.description:
                description = meta.description
        descriptions[name] = description
    return descriptions


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


async def test_download_output_failed_is_validation_with_reason(
    runtime: Runtime, mock_api: MockAPI
) -> None:
    """A failed generation is refused with VALIDATION (CONTRACTS.md B5:
    any status != succeeded), stating the status and the provider reason."""
    _set_generation_status(mock_api, "failed", error="NSFW content detected")

    result = await download_output("gen-123")

    assert result["error"]["code"] == "VALIDATION"
    assert "failed" in result["error"]["message"]
    details = result["error"]["details"]
    assert details["status"] == "failed"
    assert details["provider_reason"] == "NSFW content detected"
    assert _cdn_requests(mock_api) == []


async def test_download_output_traversal_id_cannot_escape_dest_dir(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """A traversal-shaped generation_id is sanitized: files stay in dest_dir.

    Regression: the filename stem was built from the raw generation_id, so a
    hostile/compromised gateway confirming ``../../evil`` as succeeded made
    the download escape two directories above the requested dest_dir.
    """
    evil_id = "../../evil"
    body = _generation_body("succeeded")
    body["id"] = evil_id
    mock_api.on("GET", "/evil", lambda _req: httpx.Response(200, json=body))

    dest = tmp_path / "deep" / "deeper"
    result = await download_output(evil_id, dest_dir=str(dest))

    assert "error" not in result or not isinstance(result["error"], dict)
    assert len(result["files"]) == 1
    written = Path(result["files"][0]).resolve()
    assert written.is_file()
    assert written.parent == dest.resolve(), "file must stay inside dest_dir"
    assert dest.resolve() in written.parents or written.parent == dest.resolve()
    # nothing may have been written outside dest_dir
    escaped = [p for p in tmp_path.rglob("*") if p.is_file() and dest.resolve() not in p.resolve().parents]
    assert escaped == []


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


@pytest.mark.parametrize(
    "source",
    [
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        "DATA:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        "  data:text/plain,hello",
    ],
)
async def test_upload_media_data_url_is_validation(
    runtime: Runtime, mock_api: MockAPI, source: str
) -> None:
    """v1.1 addendum #7: data: URLs are rejected with guidance, no upload made."""
    result = await upload_media(source)

    assert result["error"]["code"] == "VALIDATION"
    message = result["error"]["message"]
    assert "data:" in message
    assert "directly" in message
    assert "http" in message
    assert _media_requests(mock_api) == []


async def test_download_output_dest_dir_occupied_by_file_is_validation(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """v1.1 addendum #7: a dest_dir that exists as a FILE maps to VALIDATION."""
    occupied = tmp_path / "not-a-dir"
    occupied.write_bytes(b"occupied")

    result = await download_output("gen-123", dest_dir=str(occupied))

    assert result["error"]["code"] == "VALIDATION"
    assert "dest_dir" in result["error"]["message"]
    assert result["error"]["details"]["dest_dir"] == str(occupied)
    assert _cdn_requests(mock_api) == []


async def test_download_output_dest_dir_under_file_is_validation(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """v1.1 addendum #7: mkdir OSError (parent is a file) maps to VALIDATION."""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"file, not dir")

    result = await download_output("gen-123", dest_dir=str(blocker / "child"))

    assert result["error"]["code"] == "VALIDATION"
    assert "dest_dir" in result["error"]["message"]
    assert _cdn_requests(mock_api) == []


async def test_download_output_strips_whitespace_and_backticks(
    runtime: Runtime, mock_api: MockAPI, tmp_path: Path
) -> None:
    """v1.1 addendum #3: generation_id is stripped of whitespace and backticks."""
    dest = tmp_path / "dl"

    result = await download_output("\t `gen-123` \n", dest_dir=str(dest))

    assert "error" not in result
    assert result["generation_id"] == "gen-123"
    assert len(result["files"]) == 1
    assert Path(result["files"][0]).name == "gen-123-0.png"

    generation_requests = [
        req for req in mock_api.requests if "/generations/" in req.url.path
    ]
    assert len(generation_requests) == 1
    assert generation_requests[0].url.path.endswith("/generations/gen-123")


def test_every_media_tool_parameter_has_field_description() -> None:
    """v1.1 addendum #1: every parameter carries a short Field description."""
    for tool in (upload_media, download_output):
        descriptions = _field_descriptions(tool)
        assert descriptions, f"{tool.__name__} has no parameters to describe?"
        for name, description in descriptions.items():
            assert description, f"{tool.__name__}({name}) is missing a Field description"
            assert len(description) <= 120, (
                f"{tool.__name__}({name}) description exceeds 120 chars"
            )

    # source explains the data:-URL and local-path handling.
    source_description = _field_descriptions(upload_media)["source"]
    assert source_description is not None
    assert "data:" in source_description

    # dest_dir names the env default.
    dest_dir_description = _field_descriptions(download_output)["dest_dir"]
    assert dest_dir_description is not None
    assert "PIXIO_DOWNLOAD_DIR" in dest_dir_description


def test_media_tools_annotate_dict_str_any_return() -> None:
    """v1.1 addendum #4: tools declare -> dict[str, Any] uniformly."""
    for tool in (upload_media, download_output):
        sig = inspect.signature(tool, eval_str=True)
        assert sig.return_annotation == dict[str, Any], tool.__name__


def test_media_docstrings_have_no_args_section() -> None:
    """v1.1 addendum #4: Args: sections are gone (Field descriptions replace them)."""
    for tool in (upload_media, download_output):
        doc = inspect.getdoc(tool) or ""
        assert "Args:" not in doc, tool.__name__
