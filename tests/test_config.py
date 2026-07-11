"""Unit tests for pixio_mcp.config.Settings (contract B1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import TEST_KEY
from pixio_mcp.config import Settings
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
        }
    )
    assert settings.api_key == TEST_KEY
    assert settings.base_url == "https://alt.example/api/v1"
    assert settings.max_credits_per_job == 25
    assert settings.session_budget == 100
    assert settings.default_timeout_s == 30
    assert settings.download_dir == download_dir
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "var",
    [
        "PIXIO_MAX_CREDITS_PER_JOB",
        "PIXIO_SESSION_BUDGET",
        "PIXIO_DEFAULT_TIMEOUT_S",
    ],
)
def test_invalid_int_env_raises_validation_naming_the_var(var: str) -> None:
    env = {"PIXIO_API_KEY": TEST_KEY, var: "not-a-number"}
    with pytest.raises(PixioError) as excinfo:
        Settings.from_env(env=env)
    err = excinfo.value
    assert err.code == ErrorCode.VALIDATION
    assert var in err.to_dict()["error"]["message"]


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


def test_repr_redacts_api_key() -> None:
    # AC-7 (unit slice): the key must never appear in repr()/str().
    settings = Settings.from_env(env={"PIXIO_API_KEY": TEST_KEY})
    assert settings.api_key == TEST_KEY  # value itself remains usable
    assert TEST_KEY not in repr(settings)
    assert TEST_KEY not in str(settings)
