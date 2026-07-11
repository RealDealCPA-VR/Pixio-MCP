"""Offline tests for ``pixio_mcp.pathguard.find_local_paths``.

The guard must flag every string that looks like a local filesystem
reference (Windows drive, relative, home, file://, UNC, or an existing
on-disk path) while always allowing http(s)/data URLs and ordinary prompt
text, reporting dotted field paths for nested structures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixio_mcp.pathguard import find_local_paths


@pytest.mark.parametrize(
    "value",
    [
        "C:\\x.png",
        "C:/x.png",
        "./x.png",
        "../x",
        "~/x",
        "file://x",
        "file:///C:/photos/in.png",
        r"\\server\share\x.png",
    ],
)
def test_flags_local_path_like_value(value: str) -> None:
    """Each local-path spelling is flagged with its field name and value."""
    assert find_local_paths({"image_url": value}) == [("image_url", value)]


def test_flags_existing_file_passed_as_plain_string(tmp_path: Path) -> None:
    """A real on-disk file passed as a bare string is flagged."""
    file = tmp_path / "frame.png"
    file.write_bytes(b"\x89PNG\r\n\x1a\n")
    value = str(file)
    assert find_local_paths({"image_url": value}) == [("image_url", value)]


@pytest.mark.parametrize(
    "value",
    [
        "https://cdn.example/out.png",
        "http://cdn.example/out.png",
        "data:image/png;base64,iVBORw0KGgo=",
        "a cozy cabin at dusk, ultra detailed, 4k",
        "make the sky more dramatic",
    ],
)
def test_allows_remote_urls_and_prompt_text(value: str) -> None:
    """http(s)/data URIs and ordinary prompt text are never flagged."""
    assert find_local_paths({"prompt": value}) == []


def test_non_string_values_are_ignored() -> None:
    """Numbers, booleans, and None can never be local paths."""
    params = {"steps": 4, "strength": 0.5, "enabled": True, "seed": None}
    assert find_local_paths(params) == []


def test_empty_params_yield_no_hits() -> None:
    """An empty params dict produces no findings."""
    assert find_local_paths({}) == []


def test_nested_traversal_produces_dotted_paths() -> None:
    """A hit inside dict -> list nesting is reported as image.urls[1]."""
    params = {
        "prompt": "ok",
        "image": {"urls": ["https://cdn.example/a.png", "C:\\bad.png"]},
    }
    assert find_local_paths(params) == [("image.urls[1]", "C:\\bad.png")]


def test_list_of_dicts_traversal_produces_indexed_dotted_paths() -> None:
    """A hit inside list -> dict nesting is reported as inputs[0].ref."""
    params = {"inputs": [{"ref": "./frame.png"}]}
    assert find_local_paths(params) == [("inputs[0].ref", "./frame.png")]


def test_multiple_offenders_all_reported() -> None:
    """Every offending field is reported, clean siblings are not."""
    params = {
        "a": "./a.png",
        "b": {"c": "~/b.png"},
        "d": ["http://cdn.example/ok.png", "file://x"],
        "prompt": "a friendly robot",
    }
    hits = set(find_local_paths(params))
    assert hits == {
        ("a", "./a.png"),
        ("b.c", "~/b.png"),
        ("d[1]", "file://x"),
    }
