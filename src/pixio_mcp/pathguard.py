"""Local-filesystem-path detection for generation params.

The ``generate`` tool has a URLs-only contract: every media input inside its
``params`` object must be an http(s) or data: URL (produced by
``upload_media``). This module finds any string in an arbitrarily nested
params structure that looks like a local filesystem reference so the caller
can be rejected *before* any credits are spent.
"""

from __future__ import annotations

import os
import re
from typing import Any

__all__ = ["find_local_paths"]

#: URL schemes that are always allowed and never treated as local paths.
_ALLOWED_PREFIXES: tuple[str, ...] = ("http://", "https://", "data:")

#: Windows drive-letter prefix, e.g. ``C:\`` or ``C:/``.
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: Strings at or above this length are never probed with ``os.path.exists``.
_MAX_EXISTS_CHECK_LEN = 500


def _is_local_reference(value: str) -> bool:
    """Return True if *value* looks like a local filesystem reference.

    Allowed outright (returns False): strings starting with ``http://``,
    ``https://``, or ``data:`` (case-insensitive).

    Flagged (returns True): strings starting with ``~``, ``./``, ``../``,
    ``file://`` (case-insensitive), a Windows drive letter (``X:\\`` or
    ``X:/``), or UNC ``\\\\``; and, as a last resort, strings shorter than
    500 characters that exist on disk (``os.path.exists``).
    """
    lowered = value.lower()
    if lowered.startswith(_ALLOWED_PREFIXES):
        return False
    if value.startswith(("~", "./", "../", "\\\\")):
        return True
    if lowered.startswith("file://"):
        return True
    if _DRIVE_RE.match(value):
        return True
    if len(value) < _MAX_EXISTS_CHECK_LEN and os.path.exists(value):
        return True
    return False


def _walk(node: Any, prefix: str, hits: list[tuple[str, str]]) -> None:
    """Recursively collect (dotted.field.path, value) local-path hits."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_s = str(key)
            _walk(value, f"{prefix}.{key_s}" if prefix else key_s, hits)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, f"{prefix}[{index}]", hits)
    elif isinstance(node, str) and _is_local_reference(node):
        hits.append((prefix, node))


def find_local_paths(params: dict) -> list[tuple[str, str]]:
    """Find every string in *params* that looks like a local filesystem path.

    Walks nested dicts and lists recursively. Returns a list of
    ``(dotted.field.path, offending_value)`` tuples in encounter order,
    where dict keys are joined with ``.`` and list positions are rendered
    as ``[i]`` (e.g. ``image_urls[0]`` or ``input.frames[2].url``).

    A string is flagged when it starts with ``~``, ``./``, ``../``,
    ``file://``, a Windows drive letter (``X:\\`` / ``X:/``), or UNC
    ``\\\\``, or when it is shorter than 500 characters and exists on disk.
    Strings starting with ``http://``, ``https://``, or ``data:`` are
    always allowed. An empty result means *params* is URL-clean.
    """
    hits: list[tuple[str, str]] = []
    _walk(params, "", hits)
    return hits
