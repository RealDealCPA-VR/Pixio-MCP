"""MCP media tools: upload media to Pixio and download generation outputs.

``upload_media`` turns a local file or a remote link into a permanent, public
Pixio URL suitable for ``generate``'s media params; ``download_output`` saves
the output files of a succeeded generation to local disk.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from pixio_mcp.errors import ErrorCode, PixioError, tool_guard
from pixio_mcp.runtime import get_runtime

if TYPE_CHECKING:
    from pixio_mcp.client import PixioClient

logger = logging.getLogger(__name__)

_HTTP_PREFIXES: tuple[str, str] = ("http://", "https://")

#: A URL path suffix is trusted as a file extension only if it looks like one.
_URL_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,6}$")

#: Bytes of file header needed to identify every format in the sniff table.
_MAGIC_SNIFF_LEN = 16

#: Characters allowed in an output-filename stem; anything else (path
#: separators, dots, ...) is replaced so a hostile generation_id can never
#: traverse out of dest_dir.
_STEM_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_stem_prefix(generation_id: str) -> str:
    """Return the first 8 chars of *generation_id* made filesystem-safe.

    Path separators, dots, and any other non ``[A-Za-z0-9_-]`` characters are
    replaced with ``_`` so the resulting filename stem can never escape the
    destination directory (e.g. a traversal-shaped id like ``../../evil``).
    """
    return _STEM_UNSAFE_RE.sub("_", generation_id[:8]) or "gen"


def _is_http_url(value: str) -> bool:
    """Return True if *value* is an http(s) URL (case-insensitive scheme)."""
    return value.lower().startswith(_HTTP_PREFIXES)


def _file_name_from_url(url: str) -> str:
    """Extract the basename of a URL's path (percent-decoded, query ignored)."""
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    return name or "media"


def _ext_from_url(url: str) -> str | None:
    """Infer a file extension from the URL path suffix, or None if unusable."""
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix and _URL_SUFFIX_RE.fullmatch(suffix):
        return suffix
    return None


def _ext_from_magic(head: bytes) -> str | None:
    """Identify a media type from leading file bytes.

    Covers the same format set the contract maps from Content-Type
    (png/jpg/mp4/mp3/wav/webp/gif); the pinned ``PixioClient.download``
    signature returns only a byte count, so the response Content-Type header
    is not observable here and content identification happens on disk.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(head) >= 12 and head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return ".webp"
        if head[8:12] == b"WAVE":
            return ".wav"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".mp4"
    if head.startswith(b"ID3"):
        return ".mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ".mp3"
    return None


def _collect_output_urls(generation: dict[str, Any]) -> list[str]:
    """Build the ordered-unique output URL list from a raw generation record.

    Order matches the contract's job-result shape: ``outputUrl`` first, then
    every value of the ``outputs`` object that is an http(s) URL, de-duplicated
    while preserving first-seen order.
    """
    candidates: list[Any] = [generation.get("outputUrl")]
    outputs = generation.get("outputs")
    if isinstance(outputs, dict):
        candidates.extend(outputs.values())
    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, str) and _is_http_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


async def _download_one(client: PixioClient, url: str, dest_dir: Path, stem: str) -> Path:
    """Download *url* into *dest_dir* as ``{stem}{ext}`` and return the path.

    Extension resolution: the URL path suffix when it looks like a file
    extension; otherwise the file is fetched to a temporary ``.part`` name,
    its leading bytes are sniffed (png/jpg/mp4/mp3/wav/webp/gif), and the file
    is renamed — falling back to ``.bin`` when unidentifiable. The temporary
    file is removed if the download fails.
    """
    ext = _ext_from_url(url)
    if ext is not None:
        final = dest_dir / f"{stem}{ext}"
        await client.download(url, final)
        return final.resolve()

    tmp = dest_dir / f"{stem}.part"
    try:
        await client.download(url, tmp)
        with tmp.open("rb") as fh:
            head = fh.read(_MAGIC_SNIFF_LEN)
        final = dest_dir / f"{stem}{_ext_from_magic(head) or '.bin'}"
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final.resolve()


@tool_guard
async def upload_media(source: str) -> dict[str, Any]:
    """Upload a local file or mirror a remote URL to Pixio, returning a permanent public media URL.

    Use this before ``generate`` whenever a model parameter needs media
    (``image_url``, ``video_url``, ``audio_url``, ...): generate accepts
    http(s) URLs only and rejects local filesystem paths. The returned
    ``url`` (hosted on pixiomedia.nyc3.digitaloceanspaces.com) is permanent
    and publicly readable, and is exactly what generate's media params
    (e.g. ``image_url``) expect — pass it through verbatim.

    Args:
        source: An http(s) URL (mirrored server-side into Pixio storage) or a
            local file path (``~`` is expanded; uploaded as multipart).
            Directories are rejected.

    Returns:
        ``{"url": str, "source_kind": "local_file" | "remote_url",
        "file_name": str, "size_bytes": int | None}`` — ``size_bytes`` is
        None for remote URLs (the file never transits this machine).
    """
    rt = get_runtime()

    if _is_http_url(source):
        url = await rt.client.upload_url(source)
        file_name = _file_name_from_url(source)
        logger.info(
            "upload_media mirrored remote url",
            extra={"source_kind": "remote_url", "file_name": file_name},
        )
        return {
            "url": url,
            "source_kind": "remote_url",
            "file_name": file_name,
            "size_bytes": None,
        }

    path = Path(source).expanduser()
    if not path.exists():
        raise PixioError(
            ErrorCode.VALIDATION,
            f"local file not found: '{path}'. Check that the path is spelled "
            "correctly and the file exists, or pass an http(s) URL instead.",
            details={"source": source, "path": str(path)},
        )
    if not path.is_file():
        raise PixioError(
            ErrorCode.VALIDATION,
            f"'{path}' exists but is not a file (directories cannot be "
            "uploaded). Check the path and point upload_media at a single "
            "media file or an http(s) URL.",
            details={"source": source, "path": str(path)},
        )

    url = await rt.client.upload_file(path)
    size_bytes = path.stat().st_size
    logger.info(
        "upload_media uploaded local file",
        extra={"source_kind": "local_file", "file_name": path.name, "size_bytes": size_bytes},
    )
    return {
        "url": url,
        "source_kind": "local_file",
        "file_name": path.name,
        "size_bytes": size_bytes,
    }


@tool_guard
async def download_output(generation_id: str, dest_dir: str | None = None) -> dict[str, Any]:
    """Download every output file of a succeeded generation to a local directory.

    Call this after ``generate(wait=true)`` or ``wait_for_generation`` reports
    status ``succeeded``. Output URLs may be signed and expire (~1 hour), so
    download promptly. If the generation is still processing this returns a
    VALIDATION error — call ``wait_for_generation(generation_id)`` first and
    retry once it succeeds. Any non-succeeded status (including ``failed``)
    returns a VALIDATION error stating the current status; for a failed
    generation the provider's reason is included in the details.

    Args:
        generation_id: The id returned by ``generate``.
        dest_dir: Target directory, created if missing (``~`` is expanded).
            Defaults to the server's configured download directory
            (``PIXIO_DOWNLOAD_DIR``, default ``~/pixio-outputs``).

    Returns:
        ``{"generation_id": str, "files": [absolute file paths], "dest_dir": str}``
        — files are named ``{generation_id[:8]}-{index}{ext}`` (id prefix
        sanitized to filesystem-safe characters) with the extension inferred
        from each URL or the downloaded content.
    """
    rt = get_runtime()

    generation = await rt.client.get_generation(generation_id)
    status = str(generation.get("status") or "unknown")

    if status == "failed":
        # CONTRACTS.md B5: any status != succeeded is a VALIDATION error
        # stating the current status (the provider reason rides in details).
        reason = generation.get("error")
        raise PixioError(
            ErrorCode.VALIDATION,
            f"generation '{generation_id}' is not downloadable: status is "
            f"'failed' ({reason or 'no reason provided by the provider'}).",
            details={
                "generation_id": generation_id,
                "status": status,
                "provider_reason": reason,
            },
        )
    if status != "succeeded":
        raise PixioError(
            ErrorCode.VALIDATION,
            f"generation '{generation_id}' is not downloadable yet: status is "
            f"'{status}'. Call wait_for_generation('{generation_id}') first "
            "and download once it reports 'succeeded'.",
            details={"generation_id": generation_id, "status": status},
        )

    urls = _collect_output_urls(generation)
    if not urls:
        raise PixioError(
            ErrorCode.VALIDATION,
            f"generation '{generation_id}' succeeded but exposes no http(s) "
            "output URLs, so there is nothing to download.",
            details={
                "generation_id": generation_id,
                "outputs": generation.get("outputs") if isinstance(generation.get("outputs"), dict) else {},
            },
        )

    base = Path(dest_dir).expanduser() if dest_dir is not None else rt.settings.download_dir
    base.mkdir(parents=True, exist_ok=True)

    stem_prefix = _safe_stem_prefix(generation_id)
    files: list[str] = []
    for index, url in enumerate(urls):
        saved = await _download_one(rt.client, url, base, f"{stem_prefix}-{index}")
        files.append(str(saved))

    logger.info(
        "download_output saved files",
        extra={"generation_id": generation_id, "file_count": len(files)},
    )
    return {
        "generation_id": generation_id,
        "files": files,
        "dest_dir": str(base.resolve()),
    }
