"""Configuration loading via pydantic-settings + YAML."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """XClaw application configuration.

    Values are loaded in this priority order:
    1. Environment variables (prefixed with XCLAW_)
    2. xclaw.config.yaml (or path from XCLAW_CONFIG env var)
    3. Defaults defined here
    """

    model_config = SettingsConfigDict(
        env_prefix="XCLAW_",
        env_file=".env",
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    llm_provider: str = "anthropic"
    api_key: str = ""
    model: str = "claude-opus-4-5"
    base_url: str = ""
    temperature: float | None = None
    timeout: float = 120.0
    thinking: bool | None = None
    max_tokens: int = 4096
    max_tool_iterations: int = 10

    # ── Web channel ──────────────────────────────────────────────────────────
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_auth_token: str = ""

    # ── Feishu (飞书) ─────────────────────────────────────────────────────────
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # ── WeCom (企业微信) ──────────────────────────────────────────────────────
    wecom_enabled: bool = False
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""

    # ── DingTalk (钉钉) ───────────────────────────────────────────────────────
    dingtalk_enabled: bool = False
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_robot_code: str = ""

    # ── WeChat via iLink (二维码登录 + 私聊 Bot) ───────────────────────────────
    wechat_enabled: bool = False
    wechat_base_url: str = "https://ilinkai.weixin.qq.com"
    wechat_qr_total_timeout_seconds: int = 480
    wechat_qr_poll_timeout_seconds: int = 35
    wechat_qr_poll_interval_seconds: int = 1
    wechat_poll_timeout_ms: int = 25_000
    wechat_max_reply_chars: int = 1_500
    wechat_invite_refresh_seconds: int = 45
    wechat_invite_session_total_timeout_seconds: int = 90

    # ── WeChat Official Account / Mini Program (微信公众号 / 小程序) ──────────
    wechat_mp_enabled: bool = False
    wechat_mp_app_id: str = ""
    wechat_mp_app_secret: str = ""
    wechat_mp_token: str = ""             # server-config token for signature verify
    wechat_mp_encoding_aes_key: str = ""  # optional AES key for encrypted mode

    # ── QQ Group (QQ 群) ──────────────────────────────────────────────────────
    qq_enabled: bool = False
    qq_app_id: str = ""
    qq_app_secret: str = ""

    # ── Session management ────────────────────────────────────────────────────
    max_session_messages: int = 40
    compact_keep_recent: int = 20
    max_history_messages: int = 50
    memory_token_budget: int = 1500

    # ── Storage ───────────────────────────────────────────────────────────────
    data_dir: str = "./xclaw.data"
    timezone: str = "Asia/Shanghai"

    # ── Stock / Investment ────────────────────────────────────────────────────
    stock_market_default: str = "CN"
    stock_data_source: str = "akshare"

    # ── Security ──────────────────────────────────────────────────────────────
    control_chat_ids: list[str] = Field(default_factory=list)
    bash_enabled: bool = False
    rate_limit_per_minute: int = 20
    multi_user_mode: bool = False  # when True, web chat_id is scoped by auth token

    # ── MCP tool federation ────────────────────────────────────────────────────
    mcp_servers: list[dict] = Field(default_factory=list)

    # ── MCP server (expose tools to external MCP clients) ─────────────────────
    mcp_server_enabled: bool = False

    # ── Skills system ──────────────────────────────────────────────────────────
    # Pass None / ["all"] for all built-in skills; pass [] to disable all.
    enabled_skills: list[str] = Field(default_factory=lambda: ["all"])
    skills_dir: str = ""  # optional directory for custom skill .py files

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, v: str) -> str:
        allowed = {"anthropic", "openai", "deepseek", "ollama"}
        if v not in allowed:
            raise ValueError(f"llm_provider must be one of {allowed}")
        return v

    @field_validator("stock_market_default")
    @classmethod
    def validate_market(cls, v: str) -> str:
        allowed = {"CN", "US", "HK"}
        if v not in allowed:
            raise ValueError(f"stock_market_default must be one of {allowed}")
        return v

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v: float | None) -> float | None:
        if v is not None and not 0 <= v <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return v

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout must be greater than 0")
        return v

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "xclaw.db"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"

    @property
    def groups_path(self) -> Path:
        return self.data_path / "groups"

    @property
    def wechat_account_path(self) -> Path:
        return self.data_path / "wechat_account.json"

    @property
    def wechat_state_path(self) -> Path:
        return self.data_path / "wechat_state.json"


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from YAML file (if found) merged with environment variables.

    Priority: env vars > YAML file > defaults.
    """
    yaml_values: dict[str, Any] = {}

    # Determine config file path
    if config_path is None:
        config_path = Path(os.environ.get("XCLAW_CONFIG", "xclaw.config.yaml"))
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        yaml_values = {k: v for k, v in loaded.items() if v is not None}

    return Settings(**yaml_values)


# Module-level singleton; tests can call load_settings() to get a fresh copy.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings singleton (loaded on first call)."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Reset the global settings singleton (used in tests)."""
    global _settings
    _settings = None
