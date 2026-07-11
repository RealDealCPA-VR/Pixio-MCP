"""Tests for the assembled MCP surface in ``pixio_mcp.server``.

Importing the module must not run ``main()`` (no stdio loop, no env
requirements); it only builds the FastMCP instance with the 9 contract
tools registered, each carrying a caller-facing description.
"""

from __future__ import annotations

# Import only — main() must never run at import time.
import pixio_mcp.server as server

EXPECTED_TOOLS = frozenset(
    {
        "list_models",
        "get_model_params",
        "estimate_cost",
        "upload_media",
        "generate",
        "get_generation",
        "wait_for_generation",
        "download_output",
        "get_credits",
    }
)


def test_import_exposes_server_without_running_main() -> None:
    """The module imports cleanly and exposes a callable main entry point."""
    assert callable(server.main)
    assert server.mcp.name == "pixio"


async def test_lists_exactly_the_nine_contract_tools() -> None:
    """The MCP surface is exactly the 9 tools pinned by the contract."""
    tools = await server.mcp.list_tools()
    assert {tool.name for tool in tools} == set(EXPECTED_TOOLS)


async def test_every_tool_has_a_nonempty_description() -> None:
    """Docstrings are the descriptions LLM callers see — none may be empty."""
    for tool in await server.mcp.list_tools():
        assert tool.description and tool.description.strip(), (
            f"tool {tool.name!r} has no description"
        )


async def test_generate_description_states_urls_only_contract() -> None:
    """generate's description must point at upload_media and the confirm gate."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["generate"].description or ""
    assert "upload_media" in description
    assert "confirm" in description


async def test_list_models_input_schema_exposes_filter_params() -> None:
    """list_models advertises its type/query/limit/offset filters."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    schema = tools["list_models"].inputSchema
    properties = schema.get("properties", {})
    assert {"type", "query", "limit", "offset"} <= set(properties)
