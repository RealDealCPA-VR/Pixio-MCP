"""Tests for the assembled MCP surface in ``pixio_mcp.server``.

Importing the module must not run ``main()`` (no stdio loop, no env
requirements); it only builds the FastMCP instance with the 9 contract
tools registered, each carrying a caller-facing description.
"""

from __future__ import annotations

import inspect

# Import only — main() must never run at import time.
import pixio_mcp.server as server
from pixio_mcp.tools.catalog import get_model_params, list_models
from pixio_mcp.tools.credits import estimate_cost, get_credits
from pixio_mcp.tools.generation import generate, get_generation, wait_for_generation
from pixio_mcp.tools.media import download_output, upload_media

TOOL_FUNCTIONS = {
    "list_models": list_models,
    "get_model_params": get_model_params,
    "estimate_cost": estimate_cost,
    "upload_media": upload_media,
    "generate": generate,
    "get_generation": get_generation,
    "wait_for_generation": wait_for_generation,
    "download_output": download_output,
    "get_credits": get_credits,
}

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


async def test_descriptions_are_registered_dedented_via_getdoc() -> None:
    """Descriptions equal inspect.getdoc() — dedented, no source indentation."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name, fn in TOOL_FUNCTIONS.items():
        description = tools[name].description or ""
        assert description == inspect.getdoc(fn), (
            f"tool {name!r} description is not the dedented docstring"
        )
        # inspect.getdoc strips the uniform source indentation: every
        # continuation line of the raw __doc__ starts with 4+ spaces, so a
        # dedented description must not equal the raw docstring (unless the
        # docstring is a single line and there is nothing to dedent).
        raw = fn.__doc__ or ""
        if "\n" in raw.strip():
            assert description != raw, (
                f"tool {name!r} description was registered from raw __doc__ "
                "(still carries source indentation)"
            )


async def test_generate_description_states_urls_only_contract() -> None:
    """generate's description must point at upload_media and the confirm gate,
    while staying within the ~1,500-char context-economy budget."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["generate"].description or ""
    assert "upload_media" in description
    assert "confirm" in description
    assert len(description) <= 1500, (
        f"generate description is {len(description)} chars (budget ~1500)"
    )


async def test_every_input_schema_property_has_a_description() -> None:
    """v1.1 addendum #1: every tool parameter carries a Field description."""
    for tool in await server.mcp.list_tools():
        properties = tool.inputSchema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            description = prop_schema.get("description")
            assert isinstance(description, str) and description.strip(), (
                f"{tool.name}.{prop_name} has no schema description"
            )


async def test_list_models_input_schema_exposes_filter_params() -> None:
    """list_models advertises its type/query/limit/offset filters."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    schema = tools["list_models"].inputSchema
    properties = schema.get("properties", {})
    assert {"type", "query", "limit", "offset"} <= set(properties)


async def test_list_models_default_limit_is_20() -> None:
    """v1.1 addendum #4: list_models defaults to a 20-model page."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    properties = tools["list_models"].inputSchema.get("properties", {})
    assert properties["limit"].get("default") == 20
    assert inspect.signature(list_models).parameters["limit"].default == 20
