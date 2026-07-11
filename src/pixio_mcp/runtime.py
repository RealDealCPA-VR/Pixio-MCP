"""Process-wide dependency container.

server.py builds a Runtime at boot and calls init_runtime(); tool functions
call get_runtime() at call time (never at import time). Tests inject fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pixio_mcp.budget import BudgetGuard
    from pixio_mcp.cache import TTLCache
    from pixio_mcp.client import PixioClient
    from pixio_mcp.config import Settings


@dataclass
class Runtime:
    settings: "Settings"
    client: "PixioClient"
    budget: "BudgetGuard"
    catalog_cache: "TTLCache"


_runtime: Runtime | None = None


def init_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialized — call init_runtime() first")
    return _runtime


def reset_runtime() -> None:
    global _runtime
    _runtime = None
