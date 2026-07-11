"""Model-catalog MCP tools: ``list_models`` and ``get_model_params``.

These are the discovery half of the three-call contract
(list_models -> get_model_params -> generate). The server ships with zero
hardcoded model knowledge: everything is fetched live from the Pixio gateway,
and the raw catalog is cached in the runtime TTL cache under the key
``"models"`` so repeated lookups (including the cost fallback in
``pixio_mcp.tools.credits``) share one snapshot.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pixio_mcp.errors import tool_guard
from pixio_mcp.runtime import get_runtime

_CATALOG_CACHE_KEY = "models"
_DESCRIPTION_MAX_CHARS = 200
_LIMIT_MIN = 1
_LIMIT_MAX = 200
_QUERY_FIELDS = ("id", "name", "description")

#: Characters LLM callers commonly wrap identifiers in (markdown backticks,
#: stray whitespace) — stripped at tool entry per the v1.1 input-leniency rule.
_IDENT_STRIP_CHARS = "` \t\r\n"


def _strip_identifier(value: str) -> str:
    """Strip surrounding whitespace and backticks from an id-like argument."""
    return value.strip(_IDENT_STRIP_CHARS)


async def get_cached_models() -> list[dict]:
    """Return the raw Pixio model catalog through the runtime TTL cache.

    Fetches ``GET /models`` via the runtime client on a cache miss and stores
    the raw list under the cache key ``"models"``. Shared by :func:`list_models`
    and by the catalog-cost fallback in ``pixio_mcp.tools.credits`` so every
    consumer reads the same cached snapshot.

    Raises:
        PixioError: propagated from the HTTP client on gateway failure.
    """
    rt = get_runtime()
    cached = rt.catalog_cache.get(_CATALOG_CACHE_KEY)
    if cached is not None:
        return cached
    models = await rt.client.get_models()
    rt.catalog_cache.put(_CATALOG_CACHE_KEY, models)
    return models


@tool_guard
async def list_models(
    type: Annotated[
        str | None,
        Field(
            description=(
                'Exact model type to match, e.g. "text-to-image", '
                '"image-to-image", "image-to-video", "text-to-audio".'
            )
        ),
    ] = None,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Case-insensitive substring matched over model id, name, and "
                'description, e.g. "flux".'
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Max models returned, clamped 1..200 (default 20); "
                "small-context callers should filter with type/query, "
                "not raise this."
            )
        ),
    ] = 20,
    offset: Annotated[
        int,
        Field(
            description=(
                "Number of matching models to skip, for pagination; "
                "negative values are treated as 0."
            )
        ),
    ] = 0,
) -> dict[str, Any]:
    """Browse the Pixio model catalog (550+ models) with optional filters.

    Step 1 of the three-call contract for running any generation:
    1. list_models — find a model id (filter by type and/or query).
    2. get_model_params(model_id) — fetch that model's exact input schema.
    3. generate(model_id, params) — run the job.

    Returns:
      {"models": [{"id", "name", "type", "credits", "company",
      "description"}, ...], "total_matching": <matches before pagination>,
      "returned": <len of "models">, "offset": <effective offset>}.
      "credits" is the catalog-listed cost per generation; descriptions are
      truncated to 200 characters. The catalog is cached for ~10 minutes.
    """
    catalog = await get_cached_models()

    normalized_query = query.lower() if query else None
    filtered: list[dict] = []
    for model in catalog:
        if type is not None and model.get("type") != type:
            continue
        if normalized_query is not None and not any(
            normalized_query in str(model.get(field) or "").lower()
            for field in _QUERY_FIELDS
        ):
            continue
        filtered.append(model)

    effective_limit = max(_LIMIT_MIN, min(_LIMIT_MAX, limit))
    effective_offset = max(0, offset)
    page = filtered[effective_offset : effective_offset + effective_limit]

    return {
        "models": [
            {
                "id": model.get("id"),
                "name": model.get("name"),
                "type": model.get("type"),
                "credits": model.get("credits"),
                "company": model.get("company"),
                "description": str(model.get("description") or "")[
                    :_DESCRIPTION_MAX_CHARS
                ],
            }
            for model in page
        ],
        "total_matching": len(filtered),
        "returned": len(page),
        "offset": effective_offset,
    }


@tool_guard
async def get_model_params(
    model_id: Annotated[
        str,
        Field(description='Model id from list_models, e.g. "pixio/flux-1/schnell".'),
    ],
) -> dict[str, Any]:
    """Fetch the exact input schema for one Pixio model.

    Step 2 of the three-call contract: list_models -> get_model_params ->
    generate. Build the ``params`` object for generate() strictly from this
    response — the server embeds no per-model knowledge.

    Critical gotchas when building params for generate():
    - For select-type params the allowed values are ``options[].value``
      (there is no ".values" array). Send select values as STRINGS even when
      they look numeric — e.g. "5", not 5.
    - Some params marked optional-with-default are still required by the
      gateway. On your first attempt send EVERY listed param, using each
      param's ``defaultValue`` where you have no better value.

    Returns:
      The gateway /params response verbatim: {"model": {...}, "params":
      [{"name", "type", "label", "required", "defaultValue",
      "placeholder"?, "options"?: [{"value", "label"}]}, ...]}.
      An unknown model id yields a NOT_FOUND error dict.
    """
    rt = get_runtime()
    return await rt.client.get_params(_strip_identifier(model_id))
