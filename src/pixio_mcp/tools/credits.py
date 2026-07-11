"""Credit MCP tools: ``estimate_cost`` and ``get_credits``.

Also exposes :func:`resolve_estimate`, the shared estimate -> catalog-cost ->
unknown fallback chain used by both ``estimate_cost`` and
``pixio_mcp.tools.generation`` for pre-spend budget checks. The catalog
fallback reads the same TTL-cached catalog snapshot the catalog tools use.
"""

from __future__ import annotations

import logging

from pixio_mcp.errors import ErrorCode, PixioError, tool_guard
from pixio_mcp.runtime import get_runtime
from pixio_mcp.tools.catalog import get_cached_models

_logger = logging.getLogger(__name__)

_NO_FALLBACK_CODES = frozenset({ErrorCode.AUTH, ErrorCode.INSUFFICIENT_CREDITS})

SOURCE_ESTIMATE = "estimate"
SOURCE_CATALOG = "catalog"
SOURCE_UNKNOWN = "unknown"


def _coerce_credits(value: object) -> int | None:
    """Coerce a gateway cost value to a non-negative int, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    return None


async def resolve_estimate(
    model_id: str, params: dict
) -> tuple[int | None, str, str | None]:
    """Resolve a credit estimate for a job via the standard fallback chain.

    Chain: the gateway estimate endpoint ("estimate") -> the catalog-listed
    per-generation cost from the shared cached catalog ("catalog") ->
    ("unknown", with a human-readable warning). Shared by the estimate_cost
    tool and by generate()'s pre-spend budget check.

    Args:
        model_id: Catalog model id, e.g. "pixio/flux-1/schnell".
        params: The exact params object intended for generate().

    Returns:
        ``(estimated_credits, source, warning)`` where ``source`` is one of
        "estimate", "catalog", or "unknown"; ``estimated_credits`` is None and
        ``warning`` is a non-None string only when ``source`` is "unknown".

    Raises:
        PixioError: with code AUTH or INSUFFICIENT_CREDITS from the estimate
            endpoint — those are never swallowed by the fallback chain.
    """
    estimate_failure: str
    try:
        payload = await get_runtime().client.estimate(model_id, params)
    except PixioError as exc:
        if exc.code in _NO_FALLBACK_CODES:
            raise
        estimate_failure = exc.message
    else:
        raw_cost = payload.get("estimatedCost")
        estimated = _coerce_credits(raw_cost)
        if estimated is not None:
            return estimated, SOURCE_ESTIMATE, None
        estimate_failure = f"response had no usable estimatedCost ({raw_cost!r})"

    catalog_failure: str
    try:
        catalog = await get_cached_models()
    except PixioError as exc:
        catalog_failure = exc.message
    else:
        for model in catalog:
            if model.get("id") == model_id:
                listed = _coerce_credits(model.get("credits"))
                if listed is not None:
                    _logger.debug(
                        "estimate fell back to catalog cost",
                        extra={"model_id": model_id, "credits": listed},
                    )
                    return listed, SOURCE_CATALOG, None
                catalog_failure = "catalog entry has no usable credits value"
                break
        else:
            catalog_failure = "model not found in catalog"

    warning = (
        f"Could not determine a credit cost for {model_id!r} "
        f"(estimate endpoint: {estimate_failure}; catalog fallback: "
        f"{catalog_failure}). The true cost of this job is unknown."
    )
    _logger.debug(
        "estimate unresolved", extra={"model_id": model_id, "warning": warning}
    )
    return None, SOURCE_UNKNOWN, warning


@tool_guard
async def estimate_cost(model_id: str, params: dict) -> dict:
    """Estimate the credit cost of a generation BEFORE spending anything.

    Call this after get_model_params and before generate (the three-call
    contract is list_models -> get_model_params -> generate; this tool is the
    recommended pre-flight between steps 2 and 3). Costs come from the gateway
    estimate endpoint when available ("source": "estimate"), else from the
    catalog-listed per-generation cost ("source": "catalog"), else the cost is
    reported as unknown.

    Args:
        model_id: Catalog model id, e.g. "pixio/flux-1/schnell".
        params: The exact params object you intend to pass to generate(),
            built from the get_model_params response.

    Returns:
        {"model_id": str, "estimated_credits": int | null, "source":
        "estimate" | "catalog" | "unknown"}, plus a "warning" string only when
        the cost could not be determined ("estimated_credits" is null and
        "source" is "unknown").
    """
    estimated_credits, source, warning = await resolve_estimate(model_id, params)
    result: dict = {
        "model_id": model_id,
        "estimated_credits": estimated_credits,
        "source": source,
    }
    if warning is not None:
        result["warning"] = warning
    return result


@tool_guard
async def get_credits(
    include_ledger_tail: bool = False, ledger_limit: int = 10
) -> dict:
    """Report the Pixio account's current credit balance.

    Use to check affordability before a job or to audit spend after one.
    Every terminal generate / wait_for_generation result already includes
    "remaining_balance", so this tool is mainly for standalone balance checks
    and recent-spend review.

    Args:
        include_ledger_tail: When true, also return the most recent credit
            ledger entries (spend and top-up history).
        ledger_limit: Maximum ledger entries to include (default 10; only
            used when include_ledger_tail is true; negative values are
            treated as 0).

    Returns:
        {"total": <spendable credits>, "recurring": {"current", "quota",
        "lastTopOffAt"}, "permanent": <non-expiring credits>}, plus
        "ledger_tail": [{"id", "reason", "deltaRecurring", "deltaPermanent",
        "sourceId", "createdAt"}, ...] when include_ledger_tail is true.
    """
    rt = get_runtime()
    balance = await rt.client.get_credits()
    result: dict = {
        "total": balance.get("total"),
        "recurring": balance.get("recurring"),
        "permanent": balance.get("permanent"),
    }
    if include_ledger_tail:
        result["ledger_tail"] = await rt.client.get_ledger(
            limit=max(0, ledger_limit)
        )
    return result
