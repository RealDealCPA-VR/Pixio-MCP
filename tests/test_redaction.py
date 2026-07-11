"""Secrets-hygiene tests (# AC-7).

Runs a representative tool session against the mock gateway with logging
fully active at DEBUG, then proves the API key never appears in any log
record, any serialized tool result, or ``repr(Settings)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from pixio_mcp.config import setup_logging
from pixio_mcp.tools import generation as generation_module
from pixio_mcp.tools.catalog import get_model_params, list_models
from pixio_mcp.tools.credits import estimate_cost, get_credits
from pixio_mcp.tools.generation import generate
from pixio_mcp.tools.media import upload_media

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MockAPI
    from pixio_mcp.config import Settings
    from pixio_mcp.runtime import Runtime

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _RecordCollector(logging.Handler):
    """Captures every LogRecord emitted through the loggers it is attached to.

    Used in addition to caplog so records are captured even if
    ``setup_logging`` disables propagation on the pixio_mcp logger tree.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _install_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap every ``asyncio.sleep`` at 10 ms so poll backoff never stalls tests."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay: float, result: Any = None) -> Any:
        return await real_sleep(min(float(delay), 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    if hasattr(generation_module, "sleep"):
        monkeypatch.setattr(generation_module, "sleep", _fast_sleep)


async def test_api_key_never_appears_in_logs_or_tool_results(
    runtime: Runtime,
    mock_api: MockAPI,
    settings: Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC-7: full mock session with DEBUG logging active -> the key string is
    # absent from every log record and every JSON-serialized tool result.
    key = settings.api_key
    assert key == "pxio_live_TESTSECRET123", "conftest sentinel key expected"

    setup_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="pixio_mcp")

    collector = _RecordCollector()
    pixio_logger = logging.getLogger("pixio_mcp")
    root_logger = logging.getLogger()
    pixio_logger.addHandler(collector)
    root_logger.addHandler(collector)
    try:
        _install_fast_sleep(monkeypatch)
        results: list[dict[str, Any]] = []

        listed = await list_models(type="text-to-image", query="flux")
        results.append(listed)
        assert listed["models"], "mock catalog should match the flux model"
        model_id = listed["models"][0]["id"]

        results.append(await get_model_params(model_id))
        results.append(await estimate_cost(model_id, {"prompt": "a cat"}))

        source = tmp_path / "in.png"
        source.write_bytes(PNG_MAGIC + b"\x00" * 16)
        uploaded = await upload_media(str(source))
        results.append(uploaded)

        results.append(
            await generate(
                model_id,
                {"prompt": "a cat", "image_url": uploaded["url"]},
                wait=True,
            )
        )
        results.append(await get_credits(include_ledger_tail=True))
    finally:
        pixio_logger.removeHandler(collector)
        root_logger.removeHandler(collector)

    records = list(caplog.records) + collector.records
    assert records, "the session should have emitted log records"
    for record in records:
        assert key not in record.getMessage()
        assert key not in str(vars(record))

    assert len(results) == 6
    for result in results:
        assert key not in json.dumps(result)


def test_settings_repr_redacts_api_key(settings: Settings) -> None:
    # AC-7: repr(Settings) must never expose the raw key.
    assert settings.api_key, "sentinel key must be set for this test to mean anything"
    assert settings.api_key not in repr(settings)
    assert settings.api_key not in str(settings)
