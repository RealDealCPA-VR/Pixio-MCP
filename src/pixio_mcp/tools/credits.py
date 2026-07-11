"""Credit MCP tools: ``estimate_cost`` and ``get_credits``.

Also exposes :func:`resolve_estimate`, the shared estimate -> catalog-cost ->
unknown fallback chain used by both ``estimate_cost`` and
``pixio_mcp.tools.generation`` for pre-spend budget checks, plus the shared
input-leniency helpers :func:`clean_id` and :func:`coerce_params`. The catalog
fallback reads the same TTL-cached catalog snapshot the catalog tools use.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from pydantic import Field

from pixio_mcp.errors import ErrorCode, PixioError, tool_guard
from pixio_mcp.runtime import get_runtime
from pixio_mcp.tools.catalog import get_cached_models

_logger = logging.getLogger(__name__)

_NO_FALLBACK_CODES = frozenset({ErrorCode.AUTH, ErrorCode.INSUFFICIENT_CREDITS})

SOURCE_ESTIMATE = "estimate"
SOURCE_CATALOG = "catalog"
SOURCE_UNKNOWN = "unknown"

#: Characters stripped from id-like tool arguments (LLM callers frequently
#: wrap ids in markdown backticks and stray whitespace).
_ID_STRIP_CHARS = "` \t\r\n"

#: Exact VALIDATION message for a non-object ``params`` argument (contract
#: v1.1 addendum #3).
PARAMS_TYPE_MESSAGE = "params must be a JSON object mapping parameter names to values"


def clean_id(value: str) -> str:
    """Strip surrounding whitespace and markdown backticks from an id.

    Input leniency for ``model_id`` / ``generation_id`` tool arguments:
    ``' \\`pixio/flux-1/schnell\\` '`` -> ``'pixio/flux-1/schnell'``.
    Non-string values pass through unchanged (they fail later validation).
    """
    if not isinstance(value, str):
        return value
    return value.strip(_ID_STRIP_CHARS)


def coerce_params(params: dict[str, Any] | str) -> dict[str, Any]:
    """Normalize a tool ``params`` argument to a plain dict.

    A JSON-encoded string is ``json.loads``-unwrapped up to two times (some
    LLM callers double-encode tool arguments). Anything that does not resolve
    to a JSON object raises ``PixioError(VALIDATION)`` with
    :data:`PARAMS_TYPE_MESSAGE`.
    """
    value: Any = params
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break  # not JSON at all -> falls through to the type check below
    if not isinstance(value, dict):
        raise PixioError(
            ErrorCode.VALIDATION,
            PARAMS_TYPE_MESSAGE,
            details={
                "received_type": type(params).__name__,
                "resolved_type": type(value).__name__,
            },
        )
    return value


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
    model_id: str, params: dict[str, Any]
) -> tuple[int | None, str, str | None]:
    """Resolve a credit estimate for a job via the standard fallback chain.

    Chain: the gateway estimate endpoint ("estimate") -> the catalog-listed
    per-generation cost from the shared cached catalog ("catalog") ->
    ("unknown", with a human-readable warning). Shared by the estimate_cost
    tool and by generate()'s pre-spend budget check.

    An ``estimatedCost`` of 0 is NOT treated as a usable estimate (the live
    gateway returns 0 for models it cannot price, which would bypass the
    budget caps) — it falls through to the catalog cost:

    - estimate 0 + catalog cost > 0 -> ``(catalog_cost, "catalog", warning)``
      (warning notes the 0 estimate was discarded);
    - estimate 0 + catalog cost == 0 -> ``(0, "catalog", None)`` (genuinely
      free, e.g. video-ops models);
    - estimate 0 + catalog unknown -> ``(None, "unknown", warning)``.

    Returns ``(estimated_credits, source, warning)`` where ``source`` is one
    of "estimate", "catalog", or "unknown"; ``estimated_credits`` is None
    exactly when ``source`` is "unknown".

    Raises:
        PixioError: with code AUTH or INSUFFICIENT_CREDITS from the estimate
            endpoint — those are never swallowed by the fallback chain.
    """
    estimate_was_zero = False
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
        if estimated is not None and estimated > 0:
            return estimated, SOURCE_ESTIMATE, None
        if estimated == 0:
            estimate_was_zero = True
            estimate_failure = (
                "returned estimatedCost 0, which is not a usable estimate"
            )
        else:
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
                    if estimate_was_zero and listed > 0:
                        return (
                            listed,
                            SOURCE_CATALOG,
                            (
                                f"The estimate endpoint reported 0 credits for "
                                f"{model_id!r}, which is not a usable estimate; "
                                f"using the catalog-listed cost of {listed} "
                                f"credits instead."
                            ),
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
async def estimate_cost(
    model_id: Annotated[
        str,
        Field(description='Model id from list_models, e.g. "pixio/flux-1/schnell".'),
    ],
    params: Annotated[
        dict[str, Any] | str,
        Field(
            description=(
                "Exact params object intended for generate, built from "
                "get_model_params. JSON object; JSON string accepted."
            )
        ),
    ],
) -> dict[str, Any]:
    """Estimate the credit cost of a generation BEFORE spending anything.

    Call this after get_model_params and before generate (the three-call
    contract is list_models -> get_model_params -> generate; this tool is the
    recommended pre-flight between steps 2 and 3). The cost comes from the
    gateway estimate endpoint when it returns a positive figure
    ("source": "estimate"), else from the catalog-listed per-generation cost
    ("source": "catalog"), else it is reported as unknown.

    Returns {"model_id": str, "estimated_credits": int | null, "source":
    "estimate" | "catalog" | "unknown"}, plus a "warning" string when the
    cost is uncertain (always present when "source" is "unknown").
    """
    model_id = clean_id(model_id)
    parsed_params = coerce_params(params)
    estimated_credits, source, warning = await resolve_estimate(
        model_id, parsed_params
    )
    result: dict[str, Any] = {
        "model_id": model_id,
        "estimated_credits": estimated_credits,
        "source": source,
    }
    if warning is not None:
        result["warning"] = warning
    return result


@tool_guard
async def get_credits(
    include_ledger_tail: Annotated[
        bool,
        Field(
            description=(
                "Set true to also return recent credit ledger entries "
                "(spend and top-up history)."
            )
        ),
    ] = False,
    ledger_limit: Annotated[
        int,
        Field(
            description=(
                "Max ledger entries to return (default 10); used only when "
                "include_ledger_tail is true."
            )
        ),
    ] = 10,
) -> dict[str, Any]:
    """Report the Pixio account's current credit balance.

    Use to check affordability before a job or to audit spend after one.
    Every terminal generate / wait_for_generation result already includes
    "remaining_balance", so this tool is mainly for standalone balance checks
    and recent-spend review.

    Returns {"total": <spendable credits>, "recurring": {"current", "quota",
    "lastTopOffAt"}, "permanent": <non-expiring credits>}, plus
    "ledger_tail": [{"id", "reason", "deltaRecurring", "deltaPermanent",
    "sourceId", "createdAt"}, ...] when include_ledger_tail is true.
    """
    rt = get_runtime()
    balance = await rt.client.get_credits()
    result: dict[str, Any] = {
        "total": balance.get("total"),
        "recurring": balance.get("recurring"),
        "permanent": balance.get("permanent"),
    }
    if include_ledger_tail:
        result["ledger_tail"] = await rt.client.get_ledger(
            limit=max(0, ledger_limit)
        )
    return result
