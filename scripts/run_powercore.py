"""Run the local XClaw + PowerCore stack using the existing model configuration.

Example: .venv/bin/python scripts/run_powercore.py --web-only
Only PostgreSQL runs in Docker. Model and MCP secrets stay in process environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xclaw.config import load_settings
from xclaw.powercore import mcp_catalog
from xclaw.runtime import _build_tool_registry, run


def checked(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "xclaw.config.yaml"))
    parser.add_argument("--powercore-dir", type=Path, default=ROOT.parent / "PowerCore")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--powercore-port", type=int, default=18080)
    parser.add_argument("--postgres-port", type=int, default=55439)
    parser.add_argument("--postgres-image", default="powercore-postgres:0.1.0")
    parser.add_argument("--profile", choices=["mcp-readonly", "mcp-controlled"], default="mcp-controlled")
    parser.add_argument("--web-only", action="store_true")
    args = parser.parse_args()
    core = args.powercore_dir.resolve()
    settings = load_settings(args.config)
    if settings.llm_provider not in {"openai", "deepseek", "ollama"}:
        raise SystemExit("This local launcher requires an OpenAI-compatible model configuration.")
    updates = {
        "agent_backend": "powercore", "web_enabled": True, "web_host": "127.0.0.1",
        "web_port": args.web_port, "powercore_base_url": f"http://127.0.0.1:{args.powercore_port}",
        "powercore_mcp_token": settings.powercore_mcp_token or secrets.token_urlsafe(32),
        "powercore_profile": args.profile,
    }
    if args.data_dir:
        updates["data_dir"] = str(args.data_dir.resolve())
    if args.web_only:
        updates.update({f"{channel}_enabled": False for channel in (
            "feishu", "wecom", "dingtalk", "wechat", "wechat_mp", "qq",
        )})
    settings = settings.model_copy(update=updates)
    state = settings.data_path.resolve() / "powercore"
    state.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if (core / ".cache/cargo/bin/cargo").exists():
        environment.update({
            "CARGO_HOME": str(core / ".cache/cargo"), "RUSTUP_HOME": str(core / ".cache/rustup"),
            "PATH": str(core / ".cache/cargo/bin") + os.pathsep + environment.get("PATH", ""),
        })
    cargo = shutil.which("cargo", path=environment.get("PATH"))
    if not cargo:
        raise SystemExit("Install the Rust toolchain declared in PowerCore/rust-toolchain.toml.")
    checked([cargo, "build", "--locked", "-p", "powercore-server", "-p", "powercore-worker"], cwd=core, env=environment)
    docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
    image = subprocess.run([docker, "image", "inspect", args.postgres_image], capture_output=True, text=True)
    if image.returncode:
        if args.postgres_image != "powercore-postgres:0.1.0":
            raise SystemExit(f"Cannot inspect the supplied PostgreSQL image: {image.stderr.strip()}")
        checked([docker, "build", "-t", args.postgres_image, "-f", "deploy/postgres.Dockerfile", str(core)], cwd=core)
    container = f"xclaw-powercore-postgres-{args.postgres_port}"
    existing = subprocess.run([docker, "inspect", container], capture_output=True, text=True)
    if existing.returncode:
        checked([
            docker, "run", "-d", "--name", container, "--label", "xclaw.powercore-dev=true",
            "-p", f"127.0.0.1:{args.postgres_port}:5432",
            "-e", "POSTGRES_DB=powercore", "-e", "POSTGRES_USER=powercore",
            "-v", f"{container}:/var/lib/postgresql/data", args.postgres_image,
        ])
    else:
        details = json.loads(existing.stdout)[0]
        if details["Config"].get("Labels", {}).get("xclaw.powercore-dev") != "true":
            raise SystemExit("The PostgreSQL container name is already owned by another application.")
        checked([docker, "start", container], stdout=subprocess.DEVNULL)
    for _ in range(60):
        probe = subprocess.run([docker, "exec", container, "pg_isready", "-U", "powercore"], capture_output=True)
        if not probe.returncode:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("PostgreSQL did not become ready.")

    catalog_path = state / "mcp.toml"
    registry = _build_tool_registry(settings)
    catalog_path.write_text(mcp_catalog(
        registry, settings, f"http://127.0.0.1:{args.web_port}/_powercore/mcp",
    ), encoding="utf-8")
    catalog_path.chmod(0o600)
    base_url = settings.base_url or {
        "openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com/v1",
        "ollama": "http://127.0.0.1:11434/v1",
    }[settings.llm_provider]
    config_path = state / "powercore.toml"
    config_path.write_text("\n".join([
        "[model]", 'provider = "openai_compatible"', f"base_url = {json.dumps(base_url)}",
        f"model = {json.dumps(settings.model)}", f"timeout_seconds = {int(settings.timeout)}",
        f"max_tokens = {settings.max_tokens}",
        *([f"temperature = {settings.temperature}"] if settings.temperature is not None else []),
        *([f"thinking = {str(settings.thinking).lower()}"] if settings.thinking is not None else []),
        "[server]", f'bind = "127.0.0.1:{args.powercore_port}"',
        f"runtime_root = {json.dumps(str(state / 'runtime'))}",
        "development_auth = true", "fixtures_enabled = false",
        "[worker]", f"runtime_root = {json.dumps(str(state / 'runtime'))}",
        "concurrency = 4", "[tasks]", "enabled = false", "",
    ]), encoding="utf-8")
    environment.update({
        "PC_CONFIG": str(config_path), "PC_ENV": "dev",
        "PC_DATABASE_URL": f"postgresql://powercore@127.0.0.1:{args.postgres_port}/powercore",
        "PC_MCP_CONFIG": str(catalog_path),
        "PC_MODEL_API_KEY_REF": "env:XCLAW_POWERCORE_MODEL_KEY",
        "XCLAW_POWERCORE_MODEL_KEY": settings.api_key,
        "XCLAW_POWERCORE_MCP_AUTHORIZATION": "Bearer " + settings.powercore_mcp_token,
    })
    children = []
    try:
        for name in ("server", "worker"):
            with (state / f"{name}.log").open("ab") as log:
                children.append(subprocess.Popen(
                    [str(core / "target/debug" / f"powercore-{name}")],
                    cwd=core, env=environment, stdout=log, stderr=log, start_new_session=True,
                ))
        with httpx.Client(timeout=1, trust_env=False) as client:
            for _ in range(100):
                if any(child.poll() is not None for child in children):
                    raise SystemExit(f"PowerCore stopped during startup; see {state}")
                try:
                    ready = client.get(settings.powercore_base_url + "/health/ready")
                    if ready.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise SystemExit(f"PowerCore did not become ready; see {state}")
        print(f"PowerCore: {settings.powercore_base_url}; XClaw: http://127.0.0.1:{args.web_port}", flush=True)
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        print(f"PowerCore stopped. PostgreSQL data is retained in Docker volume {container}.", flush=True)


if __name__ == "__main__":
    main()
