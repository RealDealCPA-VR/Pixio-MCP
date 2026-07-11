"""Error taxonomy for pixio-mcp.

Defines the stable, machine-actionable error codes shared by every layer
(client, budget guard, tools), the :class:`PixioError` exception that carries
them, and :func:`tool_guard` — the decorator that turns exceptions escaping an
MCP tool body into structured error dicts so nothing ever raises through to
FastMCP.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar, cast

_logger = logging.getLogger("pixio_mcp.errors")

#: Pixio API keys are ``pxio_``-prefixed tokens (e.g. ``pxio_live_...``).
#: Defensive scrub applied to unexpected exception text before it is logged or
#: returned, so the key can never leak through a stray upstream traceback.
_KEY_PATTERN = re.compile(r"pxio_[A-Za-z0-9_]+")


def _sanitize(text: str) -> str:
    """Redact anything shaped like a Pixio API key from *text*."""
    return _KEY_PATTERN.sub("pxio_[REDACTED]", text)


class ErrorCode(str, Enum):
    """Stable error codes returned in every tool error dict."""

    AUTH = "AUTH"  # 401 / missing key
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"  # 402
    VALIDATION = "VALIDATION"  # bad/missing param; local path in generate
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"  # guardrail refusal
    CONCURRENCY = "CONCURRENCY"  # 429 account in-flight limit
    GENERATION_FAILED = "GENERATION_FAILED"  # terminal failed status
    TIMEOUT_PENDING = "TIMEOUT_PENDING"  # wait timed out; job still running
    NOT_FOUND = "NOT_FOUND"  # 404 / unknown model or generation
    UPSTREAM_ERROR = "UPSTREAM_ERROR"  # 5xx / network / unparseable


class PixioError(Exception):
    """Structured error carrying an :class:`ErrorCode`, message, and details.

    Raised internally by the client, budget guard, and tool modules; converted
    to a JSON-serializable dict at the tool boundary via :meth:`to_dict` (tools
    return error dicts — they never raise through to FastMCP).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Create an error with a taxonomy *code*, human/LLM-readable *message*,
        and optional machine-actionable *details* mapping (copied; ``{}`` if
        omitted).
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}

    def to_dict(self) -> dict[str, Any]:
        """Return ``{"error": {"code": ..., "message": ..., "details": {...}}}``."""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            }
        }


ToolFunc = TypeVar("ToolFunc", bound=Callable[..., Awaitable[dict[str, Any]]])


def tool_guard(func: ToolFunc) -> ToolFunc:
    """Wrap an async MCP tool so every failure becomes a structured error dict.

    - :class:`PixioError` → ``err.to_dict()``.
    - Any other :class:`Exception` → an :data:`ErrorCode.UPSTREAM_ERROR` dict
      whose message is ``"<ExceptionClassName>: <str(exc)>"`` (API-key-shaped
      substrings scrubbed) with ``details={"exception_type": ...}``; the
      failure is also logged at ERROR to the ``pixio_mcp`` logger hierarchy.
    - :class:`BaseException` subclasses that are not :class:`Exception`
      (e.g. ``asyncio.CancelledError``) propagate untouched.

    The wrapped function's ``__name__``, ``__doc__``, and signature are
    preserved via :func:`functools.wraps` because FastMCP derives the tool's
    schema and description from them.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except PixioError as err:
            return err.to_dict()
        except Exception as exc:  # noqa: BLE001 — tool boundary must never raise
            exc_type = type(exc).__name__
            message = _sanitize(str(exc))
            _logger.error(
                "unhandled exception in tool %s: %s: %s",
                func.__name__,
                exc_type,
                message,
                extra={"tool": func.__name__, "exception_type": exc_type},
            )
            return PixioError(
                ErrorCode.UPSTREAM_ERROR,
                f"{exc_type}: {message}",
                details={"exception_type": exc_type},
            ).to_dict()

    return cast(ToolFunc, wrapper)
