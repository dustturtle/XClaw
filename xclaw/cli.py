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


# ── Skill management ─────────────────────────────────────────────────────────

@main.group()
def skill() -> None:
    """Manage XClaw skills (install, list, remove)."""


def _resolve_skills_dir(config: str) -> Path:
    """Return the skills directory from config, falling back to default."""
    from xclaw.config import load_settings

    try:
        settings = load_settings(config)
        if settings.skills_dir:
            return Path(settings.skills_dir)
        return settings.data_path / "skills"
    except Exception:
        return Path("./xclaw.data/skills")


@skill.command("install")
@click.argument("url")
@click.option("--config", default="xclaw.config.yaml", help="Path to config file")
def skill_install(url: str, config: str) -> None:
    """Install a YAML skill from a URL.

    Example:
        xclaw skill install https://example.com/skills/weather.yaml
    """
    skills_dir = _resolve_skills_dir(config)

    async def _install() -> str:
        from xclaw.skills.yaml_skill import install_skill_from_url

        return await install_skill_from_url(url, skills_dir)

    try:
        name = asyncio.run(_install())
        click.echo(f"✅ 技能 '{name}' 已安装到 {skills_dir / f'{name}.yaml'}")
        click.echo(f"   请在 enabled_skills 中添加 '{name}' 并设置 skills_dir")
    except Exception as exc:
        click.echo(f"❌ 安装失败: {exc}", err=True)
        sys.exit(1)


@skill.command("list")
@click.option("--config", default="xclaw.config.yaml", help="Path to config file")
def skill_list(config: str) -> None:
    """List installed skills (built-in + custom)."""
    from xclaw.skills import _BUILTIN_SKILLS

    click.echo("=== 内置技能 ===")
    for name, s in _BUILTIN_SKILLS.items():
        click.echo(f"  📦 {name:20s}  {s.description}")

    skills_dir = _resolve_skills_dir(config)
    if skills_dir.is_dir():
        yaml_files = sorted(skills_dir.glob("*.yaml"))
        py_files = [f for f in sorted(skills_dir.glob("*.py")) if not f.stem.startswith("_")]
        if yaml_files or py_files:
            click.echo(f"\n=== 自定义技能 ({skills_dir}) ===")
            for f in yaml_files:
                import yaml as _yaml

                try:
                    spec = _yaml.safe_load(f.read_text(encoding="utf-8"))
                    name = spec.get("name", f.stem)
                    desc = spec.get("description", "")
                    tools_count = len(spec.get("tools", []))
                    click.echo(f"  📄 {name:20s}  {desc}  ({tools_count} tool(s))")
                except Exception:
                    click.echo(f"  ⚠️  {f.name:20s}  (解析失败)")
            for f in py_files:
                click.echo(f"  🐍 {f.stem:20s}  (Python skill)")
    else:
        click.echo(f"\n  （未配置自定义技能目录，默认: {skills_dir}）")


@skill.command("remove")
@click.argument("name")
@click.option("--config", default="xclaw.config.yaml", help="Path to config file")
def skill_remove(name: str, config: str) -> None:
    """Remove an installed custom skill by name."""
    skills_dir = _resolve_skills_dir(config)

    yaml_path = skills_dir / f"{name}.yaml"
    py_path = skills_dir / f"{name}.py"

    removed = False
    if yaml_path.exists():
        yaml_path.unlink()
        click.echo(f"✅ 已删除 {yaml_path}")
        removed = True
    if py_path.exists():
        py_path.unlink()
        click.echo(f"✅ 已删除 {py_path}")
        removed = True

    if not removed:
        click.echo(f"⚠️  未找到技能 '{name}'（在 {skills_dir} 中）")
        sys.exit(1)


if __name__ == "__main__":
    main()
