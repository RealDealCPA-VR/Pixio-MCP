"""Unit tests for pixio_mcp.config.Settings and setup_logging (contract B1)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conftest import TEST_KEY
from pixio_mcp.config import ALLOWED_TRANSPORTS, Settings, setup_logging
from pixio_mcp.errors import ErrorCode, PixioError

DEFAULT_BASE_URL = "https://beta.pixio.myapps.ai/api/v1"


def test_defaults_with_empty_env() -> None:
    settings = Settings.from_env(env={})
    assert settings.api_key == ""
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.max_credits_per_job == 60
    assert settings.session_budget == 300
    assert settings.default_timeout_s == 180
    assert settings.download_dir == Path("~/pixio-outputs").expanduser()
    assert settings.log_level == "INFO"
    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


def test_every_env_override(tmp_path: Path) -> None:
    download_dir = tmp_path / "dl"
    settings = Settings.from_env(
        env={
            "PIXIO_API_KEY": TEST_KEY,
            "PIXIO_BASE_URL": "https://alt.example/api/v1",
            "PIXIO_MAX_CREDITS_PER_JOB": "25",
            "PIXIO_SESSION_BUDGET": "100",
            "PIXIO_DEFAULT_TIMEOUT_S": "30",
            "PIXIO_DOWNLOAD_DIR": str(download_dir),
            "PIXIO_LOG_LEVEL": "DEBUG",
            "PIXIO_TRANSPORT": "streamable-http",
            "PIXIO_HOST": "0.0.0.0",
            "PIXIO_PORT": "9999",
        }
    )
    assert settings.api_key == TEST_KEY
    assert settings.base_url == "https://alt.example/api/v1"
    assert settings.max_credits_per_job == 25
    assert settings.session_budget == 100
    assert settings.default_timeout_s == 30
    assert settings.download_dir == download_dir
    assert settings.log_level == "DEBUG"
    assert settings.transport == "streamable-http"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9999


@pytest.mark.parametrize(
    "var",
    [
        "PIXIO_MAX_CREDITS_PER_JOB",
        "PIXIO_SESSION_BUDGET",
        "PIXIO_DEFAULT_TIMEOUT_S",
        "PIXIO_PORT",
    ],
)
def test_invalid_int_env_raises_validation_naming_the_var(var: str) -> None:
    env = {"PIXIO_API_KEY": TEST_KEY, var: "not-a-number"}
    with pytest.raises(PixioError) as excinfo:
        Settings.from_env(env=env)
    err = excinfo.value
    assert err.code == ErrorCode.VALIDATION
    assert var in err.to_dict()["error"]["message"]


@pytest.mark.parametrize("transport", list(ALLOWED_TRANSPORTS))
def test_every_allowed_transport_accepted(transport: str) -> None:
    settings = Settings.from_env(
        env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_TRANSPORT": transport}
    )
    assert settings.transport == transport


def test_blank_transport_falls_back_to_stdio() -> None:
    settings = Settings.from_env(
        env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_TRANSPORT": "  "}
    )
    assert settings.transport == "stdio"


def test_invalid_transport_raises_validation_naming_var_and_allowed_values() -> None:
    with pytest.raises(PixioError) as excinfo:
        Settings.from_env(
            env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_TRANSPORT": "websocket"}
        )
    err = excinfo.value
    assert err.code == ErrorCode.VALIDATION
    message = err.to_dict()["error"]["message"]
    assert "PIXIO_TRANSPORT" in message
    for allowed in ALLOWED_TRANSPORTS:
        assert allowed in message
    assert err.details["allowed"] == list(ALLOWED_TRANSPORTS)


def test_invalid_port_raises_validation_naming_var() -> None:
    with pytest.raises(PixioError) as excinfo:
        Settings.from_env(env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_PORT": "eight-k"})
    err = excinfo.value
    assert err.code == ErrorCode.VALIDATION
    assert "PIXIO_PORT" in err.to_dict()["error"]["message"]


def test_host_default_and_override() -> None:
    assert Settings.from_env(env={}).host == "127.0.0.1"
    settings = Settings.from_env(
        env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_HOST": "192.168.1.50"}
    )
    assert settings.host == "192.168.1.50"


def test_positional_construction_still_works_without_new_fields() -> None:
    # Fixtures build Settings positionally; the new fields must all default.
    settings = Settings(TEST_KEY)
    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://beta.pixio.myapps.ai/api/v1", DEFAULT_BASE_URL),
        ("https://beta.pixio.myapps.ai/api/v1/", DEFAULT_BASE_URL),
        ("https://beta.pixio.myapps.ai", DEFAULT_BASE_URL),
        ("https://beta.pixio.myapps.ai/", DEFAULT_BASE_URL),
        ("https://alt.example/api/v1", "https://alt.example/api/v1"),
        ("https://alt.example", "https://alt.example/api/v1"),
    ],
)
def test_base_url_normalization(raw: str, expected: str) -> None:
    settings = Settings.from_env(
        env={"PIXIO_API_KEY": TEST_KEY, "PIXIO_BASE_URL": raw}
    )
    assert settings.base_url == expected


def test_setup_logging_enforces_json_lines_contract() -> None:
    """Regression: stderr must carry only JSON lines, path only, no dupes.

    - pixio_mcp records must not propagate into plain-text root handlers
      (they were logged twice: once as JSON, once plain).
    - httpx/httpcore log full request URLs including query strings (the
      contract is path only, never params) — capped at WARNING.
    - the mcp SDK logger is routed through the same JSON handler.
    """
    setup_logging("INFO")

    pixio_logger = logging.getLogger("pixio_mcp")
    assert pixio_logger.propagate is False
    json_handlers = [
        h for h in pixio_logger.handlers if h.get_name() == "pixio-mcp-json-stderr"
    ]
    assert len(json_handlers) == 1

    # Idempotent: a second call never duplicates the handler.
    setup_logging("DEBUG")
    assert (
        len(
            [
                h
                for h in pixio_logger.handlers
                if h.get_name() == "pixio-mcp-json-stderr"
            ]
        )
        == 1
    )

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING

    mcp_logger = logging.getLogger("mcp")
    assert mcp_logger.propagate is False
    assert any(
        h.get_name() == "pixio-mcp-json-stderr" for h in mcp_logger.handlers
    )


def test_repr_redacts_api_key() -> None:
    # AC-7 (unit slice): the key must never appear in repr()/str().
    settings = Settings.from_env(env={"PIXIO_API_KEY": TEST_KEY})
    assert settings.api_key == TEST_KEY  # value itself remains usable
    assert TEST_KEY not in repr(settings)
    assert TEST_KEY not in str(settings)
