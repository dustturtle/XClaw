"""Tests for xclaw.config module."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from xclaw.config import Settings, load_settings, reset_settings


def test_defaults():
    """Settings should have correct defaults without any config file."""
    s = Settings()
    assert s.llm_provider == "anthropic"
    assert s.web_enabled is True
    assert s.web_host == "127.0.0.1"
    assert s.web_port == 8080
    assert s.bash_enabled is False
    assert s.feishu_enabled is False
    assert s.wecom_enabled is False
    assert s.dingtalk_enabled is False


def test_load_from_yaml(tmp_path: Path):
    """load_settings() should merge YAML values."""
    config_file = tmp_path / "test.yaml"
    config_file.write_text(
        textwrap.dedent("""
            llm_provider: openai
            model: gpt-4o
            web_port: 9090
            bash_enabled: false
        """),
        encoding="utf-8",
    )
    s = load_settings(config_file)
    assert s.llm_provider == "openai"
    assert s.model == "gpt-4o"
    assert s.web_port == 9090
    assert s.bash_enabled is False


def test_load_from_yaml_missing_file():
    """load_settings() with a non-existent file should use defaults."""
    s = load_settings("/nonexistent/path.yaml")
    assert s.llm_provider == "anthropic"


def test_invalid_provider():
    """An invalid llm_provider should raise a ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(llm_provider="unsupported_provider")


def test_invalid_market():
    """An invalid stock_market_default should raise a ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(stock_market_default="INVALID")


def test_data_paths():
    """data_path, db_path, logs_path and groups_path should be correct."""
    s = Settings(data_dir="/tmp/xclaw_test")
    assert s.data_path == Path("/tmp/xclaw_test")
    assert s.db_path == Path("/tmp/xclaw_test/xclaw.db")
    assert s.logs_path == Path("/tmp/xclaw_test/logs")
    assert s.groups_path == Path("/tmp/xclaw_test/groups")


def test_env_override(monkeypatch):
    """Environment variables prefixed with XCLAW_ should override defaults."""
    monkeypatch.setenv("XCLAW_WEB_PORT", "7777")
    monkeypatch.setenv("XCLAW_LLM_PROVIDER", "deepseek")
    s = Settings()
    assert s.web_port == 7777
    assert s.llm_provider == "deepseek"


def test_reset_settings():
    """reset_settings() should clear the cached singleton."""
    reset_settings()
    from xclaw.config import get_settings, _settings

    assert _settings is None
    get_settings()  # Creates singleton
    reset_settings()
    from xclaw.config import _settings as _s2

    assert _s2 is None
