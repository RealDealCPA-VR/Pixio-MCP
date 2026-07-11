"""Unit tests for pixio_mcp.errors: PixioError.to_dict and tool_guard (B1)."""

from __future__ import annotations

import json
from typing import Any

from pixio_mcp.errors import ErrorCode, PixioError, tool_guard


def test_to_dict_shape() -> None:
    err = PixioError(ErrorCode.NOT_FOUND, "model not found", {"model_id": "x"})
    d = err.to_dict()
    assert set(d) == {"error"}
    inner = d["error"]
    assert inner["code"] == ErrorCode.NOT_FOUND
    assert inner["code"] == "NOT_FOUND"
    assert inner["message"] == "model not found"
    assert inner["details"] == {"model_id": "x"}
    json.dumps(d)  # must be JSON-serializable as-is


def test_to_dict_defaults_details_to_empty_dict() -> None:
    err = PixioError(ErrorCode.AUTH, "PIXIO_API_KEY is not set")
    inner = err.to_dict()["error"]
    assert inner["code"] == ErrorCode.AUTH
    assert inner["message"] == "PIXIO_API_KEY is not set"
    assert inner["details"] == {}


async def test_tool_guard_passes_through_success_dict() -> None:
    @tool_guard
    async def sample_tool(value: int) -> dict[str, Any]:
        """Return a success payload."""
        return {"ok": True, "value": value}

    result = await sample_tool(42)
    assert result == {"ok": True, "value": 42}


async def test_tool_guard_converts_pixio_error_to_error_dict() -> None:
    @tool_guard
    async def failing_tool() -> dict[str, Any]:
        """Always raises a PixioError."""
        raise PixioError(ErrorCode.NOT_FOUND, "generation missing", {"id": "g1"})

    result = await failing_tool()
    inner = result["error"]
    assert inner["code"] == ErrorCode.NOT_FOUND
    assert inner["message"] == "generation missing"
    assert inner["details"] == {"id": "g1"}


async def test_tool_guard_converts_unexpected_exception_to_upstream_error() -> None:
    @tool_guard
    async def exploding_tool() -> dict[str, Any]:
        """Raises a non-Pixio exception."""
        raise ValueError("boom")

    result = await exploding_tool()
    assert result["error"]["code"] == ErrorCode.UPSTREAM_ERROR
    # The exception type name must be surfaced somewhere in the error dict.
    assert "ValueError" in json.dumps(result)


def test_tool_guard_preserves_name_and_docstring() -> None:
    @tool_guard
    async def my_tool(x: int) -> dict[str, Any]:
        """My tool docstring."""
        return {"x": x}

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "My tool docstring."
