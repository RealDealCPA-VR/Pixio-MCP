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

import logging

from mcp.server.fastmcp import FastMCP

from pixio_mcp.budget import BudgetGuard
from pixio_mcp.cache import TTLCache
from pixio_mcp.client import PixioClient
from pixio_mcp.config import Settings, setup_logging
from pixio_mcp.runtime import Runtime, init_runtime
from pixio_mcp.tools.catalog import get_model_params, list_models
from pixio_mcp.tools.credits import estimate_cost, get_credits
from pixio_mcp.tools.generation import generate, get_generation, wait_for_generation
from pixio_mcp.tools.media import download_output, upload_media

_logger = logging.getLogger("pixio_mcp.server")

mcp = FastMCP("pixio")

# Register the 9 tools. tool_guard uses functools.wraps, so FastMCP sees the
# original signatures and docstrings (which are the LLM-facing descriptions).
mcp.tool()(list_models)
mcp.tool()(get_model_params)
mcp.tool()(estimate_cost)
mcp.tool()(upload_media)
mcp.tool()(generate)
mcp.tool()(get_generation)
mcp.tool()(wait_for_generation)
mcp.tool()(download_output)
mcp.tool()(get_credits)


def main() -> None:
    """Boot the pixio MCP server on stdio.

    Sequence: ``Settings.from_env()`` → ``setup_logging`` → build
    :class:`PixioClient` / :class:`BudgetGuard` (settings caps) /
    :class:`TTLCache` (10-min catalog TTL) → ``init_runtime`` → ``mcp.run()``.

    A missing ``PIXIO_API_KEY`` logs a WARNING to stderr but the server still
    boots — tools return structured AUTH errors until the key is configured.
    """
    settings = Settings.from_env()
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
    mcp.run()


if __name__ == "__main__":
    main()
