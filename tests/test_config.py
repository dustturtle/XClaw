"""Tests for xclaw.config module."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xclaw.config import Settings, load_settings, reset_settings


@pytest.fixture(autouse=True)
def isolated_config_environment(monkeypatch, tmp_path):
    for key in os.environ:
        if key.upper().startswith("XCLAW_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_defaults():
    """Settings should have correct defaults without any config file."""
    s = Settings()
    assert s.llm_provider == "openai"
    assert s.model == "glm-5-3-flash-260826"
    assert s.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert s.temperature is None
    assert s.timeout == 120.0
    assert s.thinking is None
    assert s.web_enabled is True
    assert s.web_host == "127.0.0.1"
    assert s.web_port == 8080
    assert s.bash_enabled is False
    assert s.feishu_enabled is False
    assert s.wecom_enabled is False
    assert s.dingtalk_enabled is False
    assert s.wechat_enabled is False
    assert s.wechat_invite_refresh_seconds == 45
    assert s.wechat_invite_session_total_timeout_seconds == 90
    assert s.strategy_bias_threshold == 5.0
    assert s.strategy_report_max_symbols == 10


def test_load_from_yaml(tmp_path: Path):
    """load_settings() should merge YAML values."""
    config_file = tmp_path / "test.yaml"
    config_file.write_text(
        textwrap.dedent("""
            llm_provider: openai
            model: gpt-4o
            base_url: https://example.com/v1
            temperature: 0.1
            timeout: 240.0
            thinking: true
            web_port: 9090
            bash_enabled: false
            strategy_bias_threshold: 6.5
            strategy_report_max_symbols: 12
        """),
        encoding="utf-8",
    )
    s = load_settings(config_file)
    assert s.llm_provider == "openai"
    assert s.model == "gpt-4o"
    assert s.base_url == "https://example.com/v1"
    assert s.temperature == 0.1
    assert s.timeout == 240.0
    assert s.thinking is True
    assert s.web_port == 9090
    assert s.bash_enabled is False
    assert s.strategy_bias_threshold == 6.5
    assert s.strategy_report_max_symbols == 12


def test_load_qq_accounts_from_yaml(tmp_path: Path):
    config_file = tmp_path / "qq.yaml"
    config_file.write_text(
        textwrap.dedent("""
            qq_enabled: true
            qq_accounts:
              - key: desk
                app_id: app-1
                app_secret: secret-1
                dm_enabled: true
                group_enabled: false
                typing_enabled: true
                streaming_enabled: true
              - key: ops
                app_id: app-2
                app_secret: secret-2
                group_enabled: true
                group_policy: allowlist
                allowed_group_openids:
                  - group-1
                  - group-2
        """),
        encoding="utf-8",
    )
    s = load_settings(config_file)
    assert s.qq_enabled is True
    assert len(s.qq_accounts) == 2
    assert s.qq_accounts[0].key == "desk"
    assert s.qq_accounts[0].group_enabled is False
    assert s.qq_accounts[1].group_policy == "allowlist"
    assert s.qq_accounts[1].allowed_group_openids == ["group-1", "group-2"]


def test_invalid_temperature():
    """An invalid temperature should raise a ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(temperature=2.5)


def test_invalid_timeout():
    """A non-positive timeout should raise a ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(timeout=0)


def test_load_from_yaml_missing_file():
    """load_settings() with a non-existent file should use defaults."""
    s = load_settings("/nonexistent/path.yaml")
    assert s.llm_provider == "openai"
    assert s.model == "glm-5-3-flash-260826"
    assert s.base_url == "https://ark.cn-beijing.volces.com/api/v3"


def test_example_config_uses_default_model():
    example = Path(__file__).resolve().parents[1] / "xclaw.config.example.yaml"
    settings = load_settings(example)
    assert settings.llm_provider == "openai"
    assert settings.model == "glm-5-3-flash-260826"
    assert settings.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.api_key == ""


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-4o"),
        ("openai-codex", "gpt-5.4"),
        ("anthropic", "claude-opus-4-5"),
        ("deepseek", "deepseek-chat"),
        ("ollama", "llama3"),
    ],
)
def test_other_models_do_not_inherit_ark_url(provider, model):
    settings = Settings(llm_provider=provider, model=model)
    assert settings.base_url == ""


def test_default_model_preserves_custom_url():
    settings = Settings(base_url="https://example.com/v1")
    assert settings.model == "glm-5-3-flash-260826"
    assert settings.base_url == "https://example.com/v1"


def test_env_model_override_does_not_inherit_ark_url(monkeypatch):
    monkeypatch.setenv("XCLAW_MODEL", "gpt-4o")
    assert Settings().base_url == ""


def test_env_base_url_overrides_default_model_preset(monkeypatch):
    monkeypatch.setenv("XCLAW_BASE_URL", "https://example.com/v1")
    assert Settings().base_url == "https://example.com/v1"


@pytest.mark.parametrize(
    ("wizard_input", "provider", "model", "base_url"),
    [
        ("\n\n\n\n", "openai", "glm-5-3-flash-260826", "https://ark.cn-beijing.volces.com/api/v3"),
        ("anthropic\n\n\n\n", "anthropic", "claude-opus-4-5", ""),
        ("openai\n\ngpt-4o\n\n", "openai", "gpt-4o", ""),
    ],
)
def test_setup_writes_selected_model_preset(wizard_input, provider, model, base_url):
    from xclaw.cli import main

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["setup"], input=wizard_input)
        assert result.exit_code == 0, result.output
        config = yaml.safe_load(Path("xclaw.config.yaml").read_text(encoding="utf-8"))
        assert config["llm_provider"] == provider
        assert config["model"] == model
        assert config["base_url"] == base_url
        assert config["api_key"] == ""
        assert load_settings().base_url == base_url


def test_invalid_provider():
    """An invalid llm_provider should raise a ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(llm_provider="unsupported_provider")


def test_openai_codex_provider_is_allowed():
    s = Settings(llm_provider="openai-codex")
    assert s.llm_provider == "openai-codex"


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
    assert s.auth_profiles_path == Path("/tmp/xclaw_test/auth_profiles.json")
    assert s.wechat_account_path == Path("/tmp/xclaw_test/wechat_account.json")
    assert s.wechat_state_path == Path("/tmp/xclaw_test/wechat_state.json")


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
