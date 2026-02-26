"""Tests for YAML-based declarative skill definitions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import respx
from httpx import Response

from xclaw.skills.yaml_skill import (
    HttpTool,
    YamlSkill,
    _extract_json_path,
    _render_dict,
    _render_template,
    install_skill_from_url,
    load_yaml_skill,
)
from xclaw.tools import ToolContext, ToolRegistry, ToolResult


# ── Template rendering ────────────────────────────────────────────────────────

class TestTemplateRendering:

    def test_render_param(self):
        result = _render_template("Hello {{name}}!", {"name": "World"})
        assert result == "Hello World!"

    def test_render_multiple_params(self):
        result = _render_template("{{a}} + {{b}}", {"a": "1", "b": "2"})
        assert result == "1 + 2"

    def test_render_missing_param_unchanged(self):
        result = _render_template("Hello {{missing}}", {})
        assert result == "Hello {{missing}}"

    def test_render_env_var(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY_XYZ", "secret123")
        result = _render_template("key=$TEST_KEY_XYZ", {})
        assert result == "key=secret123"

    def test_render_missing_env_var(self):
        result = _render_template("key=$DEFINITELY_NOT_SET_XYZ", {})
        assert result == "key="

    def test_render_combined(self, monkeypatch):
        monkeypatch.setenv("API_KEY_TEST", "k123")
        result = _render_template(
            "https://api.example.com?q={{query}}&key=$API_KEY_TEST",
            {"query": "test"},
        )
        assert result == "https://api.example.com?q=test&key=k123"

    def test_render_dict(self):
        d = {"url": "https://{{host}}/path", "nested": {"key": "{{val}}"}}
        result = _render_dict(d, {"host": "example.com", "val": "abc"})
        assert result["url"] == "https://example.com/path"
        assert result["nested"]["key"] == "abc"


# ── JSON path extraction ─────────────────────────────────────────────────────

class TestJsonPath:

    def test_simple_path(self):
        data = {"results": {"text": "hello"}}
        assert _extract_json_path(data, "results.text") == "hello"

    def test_array_index(self):
        data = {"items": [{"name": "a"}, {"name": "b"}]}
        assert _extract_json_path(data, "items.1.name") == "b"

    def test_empty_path(self):
        data = {"key": "value"}
        assert _extract_json_path(data, "") == data

    def test_missing_key(self):
        data = {"key": "value"}
        result = _extract_json_path(data, "missing")
        assert result == data  # returns parent when key not found


# ── HttpTool ──────────────────────────────────────────────────────────────────

class TestHttpTool:

    def test_properties(self):
        spec = {
            "name": "my_api",
            "description": "Call my API",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
            "http": {"method": "GET", "url": "https://example.com"},
        }
        tool = HttpTool(spec)
        assert tool.name == "my_api"
        assert tool.description == "Call my API"
        assert tool.parameters["required"] == ["q"]
        assert tool.risk_level.value == "medium"

    @pytest.mark.asyncio
    @respx.mock
    async def test_execute_get(self):
        respx.get("https://api.example.com/data").mock(
            return_value=Response(
                200,
                json={"result": "ok"},
                headers={"content-type": "application/json"},
            )
        )
        spec = {
            "name": "test_get",
            "description": "Test GET",
            "http": {
                "method": "GET",
                "url": "https://api.example.com/data",
            },
        }
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({}, ctx)
        assert not result.is_error
        assert "ok" in result.content

    @pytest.mark.asyncio
    @respx.mock
    async def test_execute_with_template(self):
        respx.get("https://api.example.com/search").mock(
            return_value=Response(
                200,
                json={"items": [{"title": "Result 1"}]},
                headers={"content-type": "application/json"},
            )
        )
        spec = {
            "name": "search",
            "description": "Search",
            "http": {
                "method": "GET",
                "url": "https://api.example.com/search",
                "query": {"q": "{{query}}"},
            },
        }
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({"query": "hello"}, ctx)
        assert not result.is_error

    @pytest.mark.asyncio
    @respx.mock
    async def test_execute_with_response_path(self):
        respx.get("https://api.example.com/weather").mock(
            return_value=Response(
                200,
                json={"current": {"condition": {"text": "Sunny"}}},
                headers={"content-type": "application/json"},
            )
        )
        spec = {
            "name": "weather",
            "description": "Get weather",
            "http": {
                "method": "GET",
                "url": "https://api.example.com/weather",
                "response_path": "current.condition.text",
            },
        }
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({}, ctx)
        assert not result.is_error
        assert result.content == "Sunny"

    @pytest.mark.asyncio
    async def test_execute_no_http_config(self):
        spec = {"name": "broken", "description": "No HTTP"}
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({}, ctx)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_execute_empty_url(self):
        spec = {"name": "empty_url", "http": {"url": ""}}
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({}, ctx)
        assert result.is_error

    @pytest.mark.asyncio
    @respx.mock
    async def test_execute_post_with_body(self):
        respx.post("https://api.example.com/submit").mock(
            return_value=Response(
                200,
                json={"status": "created"},
                headers={"content-type": "application/json"},
            )
        )
        spec = {
            "name": "submit",
            "description": "Submit data",
            "http": {
                "method": "POST",
                "url": "https://api.example.com/submit",
                "body": {"title": "{{title}}", "content": "{{content}}"},
            },
        }
        tool = HttpTool(spec)
        ctx = ToolContext(chat_id=1, channel="web")
        result = await tool.execute({"title": "Test", "content": "Hello"}, ctx)
        assert not result.is_error
        assert "created" in result.content


# ── YamlSkill ─────────────────────────────────────────────────────────────────

class TestYamlSkill:

    def test_basic_properties(self):
        spec = {
            "name": "my_skill",
            "description": "My test skill",
            "tools": [
                {
                    "name": "tool1",
                    "description": "Tool 1",
                    "http": {"url": "https://example.com"},
                }
            ],
        }
        skill = YamlSkill(spec)
        assert skill.name == "my_skill"
        assert skill.description == "My test skill"

    def test_register_tools(self):
        spec = {
            "name": "multi_tool",
            "description": "Multiple tools",
            "tools": [
                {
                    "name": "tool_a",
                    "description": "Tool A",
                    "http": {"url": "https://a.example.com"},
                },
                {
                    "name": "tool_b",
                    "description": "Tool B",
                    "http": {"url": "https://b.example.com"},
                },
            ],
        }
        skill = YamlSkill(spec)
        registry = ToolRegistry()
        skill.register_tools(registry, None)
        assert registry.get("tool_a") is not None
        assert registry.get("tool_b") is not None

    def test_skip_tool_without_http(self):
        spec = {
            "name": "partial",
            "description": "Partial",
            "tools": [
                {"name": "no_http", "description": "Missing HTTP config"},
                {
                    "name": "with_http",
                    "description": "Has HTTP",
                    "http": {"url": "https://example.com"},
                },
            ],
        }
        skill = YamlSkill(spec)
        registry = ToolRegistry()
        skill.register_tools(registry, None)
        assert registry.get("no_http") is None
        assert registry.get("with_http") is not None

    def test_skip_tool_without_name(self):
        spec = {
            "name": "nameless",
            "description": "Test",
            "tools": [{"description": "No name", "http": {"url": "https://example.com"}}],
        }
        skill = YamlSkill(spec)
        registry = ToolRegistry()
        skill.register_tools(registry, None)
        assert len(registry.all_tools()) == 0


# ── YAML file loading ────────────────────────────────────────────────────────

class TestLoadYamlSkill:

    def test_load_valid_file(self, tmp_path: Path):
        yaml_content = (
            "name: test_skill\n"
            "description: A test skill\n"
            "tools:\n"
            "  - name: test_tool\n"
            "    description: A test tool\n"
            "    http:\n"
            "      method: GET\n"
            "      url: https://example.com\n"
        )
        skill_file = tmp_path / "test_skill.yaml"
        skill_file.write_text(yaml_content, encoding="utf-8")

        skill = load_yaml_skill(skill_file)
        assert skill is not None
        assert skill.name == "test_skill"
        assert skill.description == "A test skill"

    def test_load_missing_name(self, tmp_path: Path):
        yaml_content = "description: No name\ntools: []\n"
        skill_file = tmp_path / "bad.yaml"
        skill_file.write_text(yaml_content, encoding="utf-8")

        skill = load_yaml_skill(skill_file)
        assert skill is None

    def test_load_invalid_yaml(self, tmp_path: Path):
        skill_file = tmp_path / "invalid.yaml"
        skill_file.write_text("{{{{not yaml", encoding="utf-8")

        skill = load_yaml_skill(skill_file)
        assert skill is None


# ── Skill loading from directory ──────────────────────────────────────────────

class TestSkillDirLoading:

    def test_yaml_skill_from_dir(self, tmp_path: Path):
        """YAML skill files in skills_dir should be discovered and loaded."""
        from xclaw.skills import SkillRegistry, _load_skills_from_dir

        yaml_content = (
            "name: weather\n"
            "description: Weather tools\n"
            "tools:\n"
            "  - name: get_weather\n"
            "    description: Get weather\n"
            "    parameters:\n"
            "      type: object\n"
            "      properties:\n"
            "        city:\n"
            "          type: string\n"
            "      required: [city]\n"
            "    http:\n"
            "      method: GET\n"
            "      url: https://api.weather.com\n"
            "      query:\n"
            "        q: '{{city}}'\n"
        )
        (tmp_path / "weather.yaml").write_text(yaml_content, encoding="utf-8")

        sr = SkillRegistry()
        _load_skills_from_dir(sr, tmp_path, ["weather"])
        assert sr.get("weather") is not None

    def test_yaml_and_py_skills_coexist(self, tmp_path: Path):
        """Both YAML and Python skill files should be loaded from the same dir."""
        from xclaw.skills import Skill, SkillRegistry, _load_skills_from_dir

        # YAML skill
        yaml_content = (
            "name: yaml_skill\n"
            "description: YAML skill\n"
            "tools:\n"
            "  - name: yaml_tool\n"
            "    description: A YAML tool\n"
            "    http:\n"
            "      url: https://example.com\n"
        )
        (tmp_path / "yaml_skill.yaml").write_text(yaml_content, encoding="utf-8")

        # Python skill
        py_content = (
            "from xclaw.skills import Skill\n"
            "from xclaw.tools import ToolRegistry\n"
            "class PySkill(Skill):\n"
            "    name = 'py_skill'\n"
            "    description = 'Python skill'\n"
            "    def register_tools(self, registry, settings):\n"
            "        pass\n"
        )
        (tmp_path / "py_skill.py").write_text(py_content, encoding="utf-8")

        sr = SkillRegistry()
        _load_skills_from_dir(sr, tmp_path, ["yaml_skill", "py_skill"])
        assert sr.get("yaml_skill") is not None
        assert sr.get("py_skill") is not None

    def test_yaml_skill_tools_registered(self, tmp_path: Path):
        """YAML skill tools should be registered into the ToolRegistry."""
        from xclaw.skills import build_skill_registry

        yaml_content = (
            "name: api_tools\n"
            "description: API tools\n"
            "tools:\n"
            "  - name: fetch_data\n"
            "    description: Fetch data\n"
            "    http:\n"
            "      url: https://example.com/data\n"
            "  - name: submit_data\n"
            "    description: Submit data\n"
            "    http:\n"
            "      method: POST\n"
            "      url: https://example.com/submit\n"
        )
        (tmp_path / "api_tools.yaml").write_text(yaml_content, encoding="utf-8")

        sr = build_skill_registry(["api_tools"], skills_dir=tmp_path)
        registry = ToolRegistry()
        sr.load_tools(registry, None)

        assert registry.get("fetch_data") is not None
        assert registry.get("submit_data") is not None
        assert registry.get("fetch_data").risk_level.value == "medium"


# ── install_skill_from_url ────────────────────────────────────────────────────

class TestInstallSkill:

    @pytest.mark.asyncio
    @respx.mock
    async def test_install_from_url(self, tmp_path: Path):
        yaml_content = (
            "name: remote_skill\n"
            "description: A remote skill\n"
            "tools:\n"
            "  - name: remote_tool\n"
            "    description: Remote tool\n"
            "    http:\n"
            "      url: https://example.com/api\n"
        )
        respx.get("https://example.com/skills/remote_skill.yaml").mock(
            return_value=Response(200, text=yaml_content)
        )

        name = await install_skill_from_url(
            "https://example.com/skills/remote_skill.yaml", tmp_path
        )
        assert name == "remote_skill"
        assert (tmp_path / "remote_skill.yaml").exists()

        # Verify the file is valid
        skill = load_yaml_skill(tmp_path / "remote_skill.yaml")
        assert skill is not None
        assert skill.name == "remote_skill"

    @pytest.mark.asyncio
    @respx.mock
    async def test_install_invalid_content(self, tmp_path: Path):
        respx.get("https://example.com/bad.yaml").mock(
            return_value=Response(200, text="not a valid skill")
        )

        with pytest.raises(ValueError, match="name"):
            await install_skill_from_url("https://example.com/bad.yaml", tmp_path)

    @pytest.mark.asyncio
    @respx.mock
    async def test_install_no_tools(self, tmp_path: Path):
        yaml_content = "name: empty\ndescription: No tools\n"
        respx.get("https://example.com/empty.yaml").mock(
            return_value=Response(200, text=yaml_content)
        )

        with pytest.raises(ValueError, match="tools"):
            await install_skill_from_url("https://example.com/empty.yaml", tmp_path)

    @pytest.mark.asyncio
    @respx.mock
    async def test_install_creates_dir(self, tmp_path: Path):
        skills_dir = tmp_path / "new_skills_dir"
        yaml_content = (
            "name: new_skill\n"
            "description: New\n"
            "tools:\n"
            "  - name: t\n"
            "    description: T\n"
            "    http:\n"
            "      url: https://example.com\n"
        )
        respx.get("https://example.com/new_skill.yaml").mock(
            return_value=Response(200, text=yaml_content)
        )

        await install_skill_from_url("https://example.com/new_skill.yaml", skills_dir)
        assert skills_dir.is_dir()
        assert (skills_dir / "new_skill.yaml").exists()


# ── CLI commands ──────────────────────────────────────────────────────────────

class TestCLI:

    def test_skill_list_command(self):
        from click.testing import CliRunner
        from xclaw.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["skill", "list"])
        assert result.exit_code == 0
        assert "内置技能" in result.output
        assert "investment" in result.output
        assert "memory" in result.output

    def test_skill_remove_nonexistent(self, tmp_path: Path):
        from click.testing import CliRunner
        from xclaw.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["skill", "remove", "nonexistent"])
        assert result.exit_code == 1
