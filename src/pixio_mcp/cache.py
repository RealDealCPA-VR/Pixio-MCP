"""In-memory TTL cache for pixio-mcp.

Used by the tool layer to cache the Pixio model catalog for 10 minutes
(see CONTRACTS.md "TTL cache"). Storage is a plain dict; expired entries
are dropped lazily on :meth:`TTLCache.get`. The clock is injectable so
tests can drive expiry deterministically with a fake monotonic clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["TTLCache"]


class TTLCache:
    """A minimal time-to-live cache backed by a dict.

    Entries expire ``ttl_s`` seconds after they were stored, measured by the
    injected ``clock``. An expired entry is removed the next time it is
    looked up. A cached value of ``None`` is indistinguishable from a miss,
    which is acceptable for the catalog payloads this cache holds.
    """

    def __init__(
        self,
        ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a cache.

        Args:
            ttl_s: Lifetime of each entry in seconds (default 10 minutes).
            clock: Zero-argument callable returning a monotonically
                increasing float timestamp; defaults to ``time.monotonic``.
                Injectable for deterministic expiry in tests.
        """
        self._ttl_s = ttl_s
        self._clock = clock
        # key -> (stored_at, value)
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key``, or ``None`` if missing/expired.

        An entry found to be expired is deleted before returning ``None``.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at >= self._ttl_s:
            del self._entries[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``, resetting its TTL.

        Overwrites any existing entry for the same key.
        """
        self._entries[key] = (self._clock(), value)

    def clear(self) -> None:
        """Drop every entry from the cache."""
        self._entries.clear()
