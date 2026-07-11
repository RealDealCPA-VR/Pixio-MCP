"""MCP server entry point for pixio-mcp.

Builds the FastMCP server named "pixio", registers the nine Pixio tools
(thin registrations of the tool-module functions, preserving their
signatures and docstrings), and provides ``main()`` — the console-script
entry point that wires Settings → logging → Runtime and runs the stdio
transport.

Nothing in this module may ever write to stdout: the MCP stdio transport
owns stdout, and all diagnostics go to stderr via the ``pixio_mcp``
JSON-lines logger configured in :func:`pixio_mcp.config.setup_logging`.
"""

from __future__ import annotations

import inspect
import logging
import sys

from mcp.server.fastmcp import FastMCP

from pixio_mcp.budget import BudgetGuard
from pixio_mcp.cache import TTLCache
from pixio_mcp.client import PixioClient
from pixio_mcp.config import Settings, setup_logging
from pixio_mcp.errors import PixioError
from pixio_mcp.runtime import Runtime, init_runtime
from pixio_mcp.tools.catalog import get_model_params, list_models
from pixio_mcp.tools.credits import estimate_cost, get_credits
from pixio_mcp.tools.generation import generate, get_generation, wait_for_generation
from pixio_mcp.tools.media import download_output, upload_media

_logger = logging.getLogger("pixio_mcp.server")

mcp = FastMCP("pixio")

# Register the 9 tools. tool_guard uses functools.wraps, so FastMCP sees the
# original signatures and docstrings. Descriptions are passed explicitly via
# inspect.getdoc() so LLM callers see them dedented (raw __doc__ keeps the
# 4-space source indentation); FastMCP's Tool.from_function uses the
# description= override only for the description text — the parameter schema
# still comes from func_metadata(fn) on the function signature.
for _tool_fn in (
    list_models,
    get_model_params,
    estimate_cost,
    upload_media,
    generate,
    get_generation,
    wait_for_generation,
    download_output,
    get_credits,
):
    mcp.tool(description=inspect.getdoc(_tool_fn))(_tool_fn)


def main() -> None:
    """Boot the pixio MCP server.

    Sequence: ``Settings.from_env()`` → ``setup_logging`` → build
    :class:`PixioClient` / :class:`BudgetGuard` (settings caps) /
    :class:`TTLCache` (10-min catalog TTL) → ``init_runtime`` →
    ``mcp.run(transport=settings.transport)``.

    ``PIXIO_TRANSPORT`` selects stdio (default), sse, or streamable-http;
    for the HTTP transports ``PIXIO_HOST``/``PIXIO_PORT`` are applied to
    FastMCP's own settings before the server starts.

    Invalid configuration (a :class:`PixioError` from ``Settings.from_env``)
    writes one clean line to stderr — no traceback — and exits 1.

    A missing ``PIXIO_API_KEY`` logs a WARNING to stderr but the server still
    boots — tools return structured AUTH errors until the key is configured.
    """
    try:
        settings = Settings.from_env()
    except PixioError as err:
        sys.stderr.write(f"{err.message}\n")
        sys.exit(1)
    setup_logging(settings.log_level)
    if not settings.api_key:
        _logger.warning(
            "PIXIO_API_KEY is not set — the server will boot, but every tool "
            "call will return an AUTH error until the key is configured."
        )
    runtime = Runtime(
        settings=settings,
        client=PixioClient(settings),
        budget=BudgetGuard(settings.max_credits_per_job, settings.session_budget),
        catalog_cache=TTLCache(600.0),
    )
    init_runtime(runtime)
    if settings.transport != "stdio":
        mcp.settings.host = settings.host
        mcp.settings.port = settings.port
        _logger.info(
            "starting pixio MCP server on %s at %s:%d",
            settings.transport,
            settings.host,
            settings.port,
            extra={
                "transport": settings.transport,
                "host": settings.host,
                "port": settings.port,
            },
        )
    mcp.run(transport=settings.transport)


if __name__ == "__main__":
    main()
