"""YAML-based declarative skill definitions.

Users can define skills by writing YAML files instead of Python code.
This enables easy creation, sharing, and importing of skill packs.

YAML Skill File Format
----------------------

.. code-block:: yaml

    name: weather
    description: "天气查询工具包"
    tools:
      - name: get_weather
        description: "获取指定城市的当前天气"
        parameters:
          type: object
          properties:
            city:
              type: string
              description: "城市名"
          required: [city]
        http:
          method: GET
          url: "https://api.weatherapi.com/v1/current.json"
          query:
            key: "$WEATHER_API_KEY"
            q: "{{city}}"
          headers:
            User-Agent: "XClaw/0.1"
          response_path: "current.condition.text"

Template Syntax
~~~~~~~~~~~~~~~
* ``{{param_name}}`` – replaced with tool parameter values
* ``$ENV_VAR``       – replaced with environment variable values

Import
~~~~~~
Skills can be installed from a URL (raw YAML file)::

    xclaw skill install https://example.com/skills/weather.yaml
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult, ToolRegistry

# Template patterns
_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")
_ENV_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


# ── Template helpers ──────────────────────────────────────────────────────────

def _render_template(template: str, params: dict[str, Any]) -> str:
    """Replace ``{{param}}`` and ``$ENV_VAR`` placeholders in *template*."""

    def _replace_param(m: re.Match) -> str:
        key = m.group(1)
        val = params.get(key)
        return str(val) if val is not None else m.group(0)

    result = _TEMPLATE_RE.sub(_replace_param, template)

    def _replace_env(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")

    return _ENV_RE.sub(_replace_env, result)


def _render_dict(d: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Recursively render template strings in a dict."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _render_template(v, params)
        elif isinstance(v, dict):
            out[k] = _render_dict(v, params)
        else:
            out[k] = v
    return out


def _extract_json_path(data: Any, path: str) -> Any:
    """Simple dot-notation JSON path extraction (e.g. ``results.0.text``)."""
    if not path:
        return data
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key, data)
        elif isinstance(data, list):
            try:
                data = data[int(key)]
            except (ValueError, IndexError):
                return data
        else:
            return data
    return data


# ── HttpTool ──────────────────────────────────────────────────────────────────

class HttpTool(Tool):
    """A tool that makes HTTP API calls based on YAML configuration."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._name: str = spec["name"]
        self._description: str = spec.get("description", "")
        self._parameters: dict[str, Any] = spec.get(
            "parameters", {"type": "object", "properties": {}}
        )
        self._http: dict[str, Any] = spec.get("http", {})

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM  # external HTTP calls are medium-risk

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        http_cfg = self._http
        if not http_cfg:
            return ToolResult(content="Tool 未配置 HTTP 执行方式", is_error=True)

        method = http_cfg.get("method", "GET").upper()
        url = _render_template(http_cfg.get("url", ""), params)
        if not url:
            return ToolResult(content="URL 为空", is_error=True)

        headers = _render_dict(http_cfg.get("headers", {}), params)
        query = _render_dict(http_cfg.get("query", {}), params)
        body = (
            _render_dict(http_cfg["body"], params)
            if isinstance(http_cfg.get("body"), dict)
            else None
        )
        response_path: str = http_cfg.get("response_path", "")
        timeout = float(http_cfg.get("timeout", 20.0))
        max_chars = int(http_cfg.get("max_chars", 8000))

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "XClaw/0.1"},
            ) as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=query,
                    json=body,
                )
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "json" in content_type:
                    data = resp.json()
                    if response_path:
                        data = _extract_json_path(data, response_path)
                    text = (
                        json.dumps(data, ensure_ascii=False, indent=2)
                        if not isinstance(data, str)
                        else data
                    )
                else:
                    text = resp.text

                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n... [截断，已显示前 {max_chars} 字符]"

                return ToolResult(content=text or "响应内容为空")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"HTTP 请求失败: {exc}", is_error=True)


# ── YamlSkill ─────────────────────────────────────────────────────────────────

class YamlSkill:
    """A skill loaded from a YAML definition file.

    Implements the same interface as :class:`~xclaw.skills.Skill` without
    requiring a Python import of the abstract base class (to avoid circular
    imports).  The :func:`build_skill_registry` loader treats it identically.
    """

    def __init__(self, spec: dict[str, Any], source_path: str = "") -> None:
        self.name: str = spec.get("name", "")
        self.description: str = spec.get("description", "")
        self._tools_spec: list[dict[str, Any]] = spec.get("tools", [])
        self._source = source_path

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        """Instantiate HttpTool instances from the YAML ``tools`` list."""
        for tool_spec in self._tools_spec:
            if not tool_spec.get("name"):
                continue
            if "http" in tool_spec:
                tool = HttpTool(tool_spec)
                try:
                    registry.register(tool)
                except ValueError:
                    pass  # already registered
            else:
                logger.warning(
                    f"YAML tool '{tool_spec.get('name')}' has no 'http' config, skipped"
                )


# ── Loader / installer ────────────────────────────────────────────────────────

def load_yaml_skill(path: Path) -> YamlSkill | None:
    """Load a :class:`YamlSkill` from a ``.yaml`` file.

    Returns ``None`` if the file is invalid or cannot be parsed.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        if not isinstance(spec, dict) or not spec.get("name"):
            logger.warning(f"Invalid YAML skill file (missing 'name'): {path}")
            return None
        return YamlSkill(spec, source_path=str(path))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to load YAML skill from {path}: {exc}")
        return None


async def install_skill_from_url(url: str, skills_dir: Path) -> str:
    """Download a YAML skill file from *url* into *skills_dir*.

    Returns the skill name on success.
    Raises ``ValueError`` if the file is invalid, ``httpx.HTTPError`` on
    network failure.
    """
    skills_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "XClaw/0.1 (skill installer)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.text

    spec = yaml.safe_load(content)
    if not isinstance(spec, dict) or not spec.get("name"):
        raise ValueError("无效的技能文件：缺少 'name' 字段")

    # Validate it has at least one tool
    tools = spec.get("tools")
    if not isinstance(tools, list) or len(tools) == 0:
        raise ValueError("无效的技能文件：缺少 'tools' 列表")

    name: str = spec["name"]
    target = skills_dir / f"{name}.yaml"
    target.write_text(content, encoding="utf-8")
    logger.info(f"Skill '{name}' installed to {target}")
    return name
