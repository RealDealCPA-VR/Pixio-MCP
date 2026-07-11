"""Unit tests for pixio_mcp.cache.TTLCache (contract B3)."""

from __future__ import annotations

from pixio_mcp.cache import TTLCache


class FakeClock:
    """Mutable stand-in for time.monotonic, injectable via TTLCache(clock=...)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_put_then_get_returns_value() -> None:
    cache = TTLCache(ttl_s=600.0)
    value = {"models": [{"id": "pixio/flux-1/schnell"}]}
    cache.put("models", value)
    assert cache.get("models") is value


def test_get_missing_key_returns_none() -> None:
    cache = TTLCache(ttl_s=600.0)
    assert cache.get("absent") is None


def test_entry_expires_after_ttl_with_fake_clock() -> None:
    clock = FakeClock(start=1000.0)
    cache = TTLCache(ttl_s=60.0, clock=clock)
    cache.put("k", "v")
    clock.now = 1059.0  # just inside the TTL
    assert cache.get("k") == "v"
    clock.now = 1061.0  # just past the TTL
    assert cache.get("k") is None


def test_put_refreshes_expiry() -> None:
    clock = FakeClock(start=0.0)
    cache = TTLCache(ttl_s=10.0, clock=clock)
    cache.put("k", "v1")
    clock.now = 8.0
    cache.put("k", "v2")
    clock.now = 16.0  # 16s after first put, only 8s after the refresh
    assert cache.get("k") == "v2"


def test_clear_removes_all_entries() -> None:
    cache = TTLCache(ttl_s=600.0)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None
