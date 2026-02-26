"""CLI entry point for XClaw."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click


@click.group()
def main() -> None:
    """XClaw – Python Agent runtime for task processing & stock investment."""


@main.command()
@click.option("--config", default="xclaw.config.yaml", help="Path to config file")
def start(config: str) -> None:
    """Start the XClaw agent runtime."""
    from xclaw.config import load_settings
    from xclaw.runtime import run

    try:
        settings = load_settings(config)
    except Exception as exc:
        click.echo(f"Failed to load config: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Starting XClaw (provider={settings.llm_provider}, model={settings.model})…")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        click.echo("\nShutting down…")


@main.command()
def setup() -> None:
    """Interactive setup wizard to create xclaw.config.yaml."""
    config_path = Path("xclaw.config.yaml")
    example_path = Path("xclaw.config.example.yaml")

    if config_path.exists():
        overwrite = click.confirm("xclaw.config.yaml already exists. Overwrite?", default=False)
        if not overwrite:
            click.echo("Setup cancelled.")
            return

    # Basic prompts
    provider = click.prompt(
        "LLM provider", default="anthropic",
        type=click.Choice(["anthropic", "openai", "deepseek", "ollama"])
    )
    api_key = click.prompt("API key", default="", hide_input=True)
    model_defaults = {
        "anthropic": "claude-opus-4-5",
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "ollama": "llama3",
    }
    model = click.prompt("Model name", default=model_defaults.get(provider, ""))

    web_port = click.prompt("Web server port", default=8080, type=int)

    lines = [
        f"llm_provider: \"{provider}\"",
        f"api_key: \"{api_key}\"",
        f"model: \"{model}\"",
        f"max_tokens: 4096",
        f"max_tool_iterations: 50",
        "",
        "web_enabled: true",
        "web_host: \"127.0.0.1\"",
        f"web_port: {web_port}",
        "web_auth_token: \"\"",
        "",
        "feishu_enabled: false",
        "wecom_enabled: false",
        "dingtalk_enabled: false",
        "",
        "data_dir: \"./xclaw.data\"",
        "timezone: \"Asia/Shanghai\"",
        "",
        "stock_market_default: \"CN\"",
        "stock_data_source: \"akshare\"",
        "",
        "bash_enabled: false",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    click.echo(f"✅ Config saved to {config_path}")


@main.command()
def doctor() -> None:
    """Check the XClaw installation and configuration."""
    click.echo("=== XClaw Doctor ===")

    # Check Python version
    import sys
    major, minor = sys.version_info[:2]
    status = "✅" if (major, minor) >= (3, 11) else "❌"
    click.echo(f"{status} Python {major}.{minor} (requires 3.11+)")

    # Check dependencies
    deps = [
        ("httpx", "httpx"),
        ("pydantic", "pydantic"),
        ("fastapi", "fastapi"),
        ("aiosqlite", "aiosqlite"),
        ("loguru", "loguru"),
        ("akshare", "akshare"),
        ("yfinance", "yfinance"),
        ("pandas_ta", "pandas_ta"),
        ("APScheduler", "apscheduler"),
    ]
    for display_name, module_name in deps:
        try:
            __import__(module_name)
            click.echo(f"✅ {display_name}")
        except ImportError:
            click.echo(f"❌ {display_name} (not installed)")

    # Check config
    config_path = Path("xclaw.config.yaml")
    if config_path.exists():
        click.echo("✅ xclaw.config.yaml found")
    else:
        click.echo("⚠️  xclaw.config.yaml not found (run 'xclaw setup')")

    click.echo("\nDone.")


if __name__ == "__main__":
    main()
