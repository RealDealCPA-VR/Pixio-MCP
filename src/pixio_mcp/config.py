"""Configuration and logging setup for pixio-mcp.

Provides :class:`Settings` (environment-driven configuration with an
injectable ``env`` mapping for tests) and :func:`setup_logging` (JSON-lines
logging to stderr — stdout belongs to the MCP stdio transport and is never
written to).

The API key is a secret: :meth:`Settings.__repr__` redacts it, and it must
never be logged or echoed in tool output anywhere in the codebase.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from pixio_mcp.errors import ErrorCode, PixioError

#: Transport names FastMCP's ``run()`` accepts (matches its own Literal type).
Transport = Literal["stdio", "sse", "streamable-http"]

_DEFAULT_BASE_URL = "https://beta.pixio.myapps.ai/api/v1"
_DEFAULT_DOWNLOAD_DIR = "~/pixio-outputs"
_API_SUFFIX = "/api/v1"

#: The allowed PIXIO_TRANSPORT values, in the order they are reported in
#: validation errors.
ALLOWED_TRANSPORTS: tuple[Transport, ...] = ("stdio", "sse", "streamable-http")


def _normalize_base_url(raw: str) -> str:
    """Normalize a base URL: no trailing slash, always ending in ``/api/v1``.

    Accepts the URL with or without a trailing slash and with or without the
    ``/api/v1`` suffix.
    """
    url = raw.strip().rstrip("/")
    if not url.endswith(_API_SUFFIX):
        url = f"{url}{_API_SUFFIX}"
    return url


def _int_env(src: Mapping[str, str], name: str, default: int) -> int:
    """Parse an integer env var; unset/blank → *default*.

    Raises:
        PixioError: :data:`ErrorCode.VALIDATION` naming the env var when the
            value is set but not a valid integer.
    """
    raw = src.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise PixioError(
            ErrorCode.VALIDATION,
            f"Environment variable {name} must be an integer, got {raw!r}.",
            details={"env_var": name, "value": raw},
        ) from None


def _transport_env(src: Mapping[str, str], name: str, default: Transport) -> Transport:
    """Parse a transport env var; unset/blank → *default*.

    Raises:
        PixioError: :data:`ErrorCode.VALIDATION` naming the env var and the
            allowed values when the value is not one of
            :data:`ALLOWED_TRANSPORTS`.
    """
    raw = src.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if value not in ALLOWED_TRANSPORTS:
        allowed = ", ".join(repr(t) for t in ALLOWED_TRANSPORTS)
        raise PixioError(
            ErrorCode.VALIDATION,
            f"Environment variable {name} must be one of {allowed}, got {value!r}.",
            details={
                "env_var": name,
                "value": value,
                "allowed": list(ALLOWED_TRANSPORTS),
            },
        )
    return cast(Transport, value)


@dataclass
class Settings:
    """Runtime configuration for the pixio-mcp server.

    ``api_key`` is ``""`` when unset — an AUTH error is raised at call time by
    the client, not at boot, so the server always starts.
    """

    api_key: str
    base_url: str = _DEFAULT_BASE_URL
    max_credits_per_job: int = 60
    session_budget: int = 300
    default_timeout_s: int = 180
    download_dir: Path = Path(_DEFAULT_DOWNLOAD_DIR)
    log_level: str = "INFO"
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        """Normalize ``base_url`` and expand ``~`` in ``download_dir``."""
        self.base_url = _normalize_base_url(self.base_url)
        self.download_dir = Path(self.download_dir).expanduser()

    def __repr__(self) -> str:
        """Debug representation with the API key redacted.

        The key renders as ``pxio_...<last4>`` (or ``<unset>`` when empty) so
        the full secret can never leak through logs or tracebacks that repr()
        this object.
        """
        key = f"pxio_...{self.api_key[-4:]}" if self.api_key else "<unset>"
        return (
            f"Settings(api_key='{key}', base_url={self.base_url!r}, "
            f"max_credits_per_job={self.max_credits_per_job}, "
            f"session_budget={self.session_budget}, "
            f"default_timeout_s={self.default_timeout_s}, "
            f"download_dir={str(self.download_dir)!r}, "
            f"log_level={self.log_level!r}, "
            f"transport={self.transport!r}, "
            f"host={self.host!r}, "
            f"port={self.port})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from *env* (defaults to ``os.environ``).

        Recognized variables: ``PIXIO_API_KEY``, ``PIXIO_BASE_URL``,
        ``PIXIO_MAX_CREDITS_PER_JOB``, ``PIXIO_SESSION_BUDGET``,
        ``PIXIO_DEFAULT_TIMEOUT_S``, ``PIXIO_DOWNLOAD_DIR``,
        ``PIXIO_LOG_LEVEL``, ``PIXIO_TRANSPORT``, ``PIXIO_HOST``,
        ``PIXIO_PORT``. Unset or blank values fall back to defaults.

        Raises:
            PixioError: :data:`ErrorCode.VALIDATION` naming the env var when
                an integer variable holds a non-integer value or
                ``PIXIO_TRANSPORT`` is not one of
                :data:`ALLOWED_TRANSPORTS`.
        """
        src: Mapping[str, str] = os.environ if env is None else env
        return cls(
            api_key=(src.get("PIXIO_API_KEY") or "").strip(),
            base_url=(src.get("PIXIO_BASE_URL") or _DEFAULT_BASE_URL).strip(),
            max_credits_per_job=_int_env(src, "PIXIO_MAX_CREDITS_PER_JOB", 60),
            session_budget=_int_env(src, "PIXIO_SESSION_BUDGET", 300),
            default_timeout_s=_int_env(src, "PIXIO_DEFAULT_TIMEOUT_S", 180),
            download_dir=Path(
                (src.get("PIXIO_DOWNLOAD_DIR") or _DEFAULT_DOWNLOAD_DIR).strip()
            ),
            log_level=(src.get("PIXIO_LOG_LEVEL") or "INFO").strip() or "INFO",
            transport=_transport_env(src, "PIXIO_TRANSPORT", "stdio"),
            host=(src.get("PIXIO_HOST") or "").strip() or "127.0.0.1",
            port=_int_env(src, "PIXIO_PORT", 8000),
        )


#: LogRecord attributes that are part of the stdlib record, not caller extras.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

_HANDLER_NAME = "pixio-mcp-json-stderr"


class _JsonLinesFormatter(logging.Formatter):
    """Compact JSON-lines formatter: ``{"ts","level","logger","msg", **extras}``.

    ``ts`` is the record's creation time in ISO-8601 UTC. Any ``extra=``
    fields supplied by the caller are merged in (non-serializable values are
    stringified); the four core keys always win on name collisions.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def _ensure_json_handler(logger: logging.Logger) -> logging.Handler:
    """Attach the shared JSON-lines stderr handler to *logger* (idempotent)."""
    for handler in logger.handlers:
        if handler.get_name() == _HANDLER_NAME:
            return handler
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(_JsonLinesFormatter())
    logger.addHandler(handler)
    return handler


def setup_logging(level: str) -> None:
    """Configure logging so ALL server output is stderr JSON lines.

    Attaches exactly one :class:`logging.StreamHandler` bound to
    ``sys.stderr`` with :class:`_JsonLinesFormatter` to the ``pixio_mcp``
    logger and sets its level from *level* (case-insensitive; unknown names
    fall back to ``INFO``). Propagation is disabled on the ``pixio_mcp``
    logger so records are never duplicated in plain text through handlers
    the MCP SDK installs on the root logger.

    Third-party logger hygiene (the logging contract is JSON lines only,
    path only — never query strings, which may carry params or signatures):

    - ``httpx`` / ``httpcore`` log full request URLs including query strings,
      so they are capped at WARNING.
    - the ``mcp`` SDK logger gets the same JSON handler (propagation off) so
      its records come out as JSON lines instead of plain text.

    Idempotent: repeated calls update the level but never add duplicate
    handlers. The root logger is never configured or given handlers — stdout
    is reserved for the MCP stdio transport and must stay untouched.
    """
    logger = logging.getLogger("pixio_mcp")
    resolved = logging.getLevelNamesMapping().get(
        (level or "").strip().upper(), logging.INFO
    )
    logger.setLevel(resolved)
    logger.propagate = False
    _ensure_json_handler(logger)

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    mcp_logger = logging.getLogger("mcp")
    mcp_logger.propagate = False
    _ensure_json_handler(mcp_logger)
