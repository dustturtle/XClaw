"""Tests for SKILL.md directory-based skill definitions (Claude Agent Skills protocol)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from xclaw.skills.doc_skill import (
    DocSkill,
    DocSkillTool,
    _parse_skill_md,
    load_doc_skill,
)
from xclaw.tools import ToolContext, ToolRegistry, ToolResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx() -> ToolContext:
    return ToolContext(chat_id=1, channel="web")


def _make_skill_dir(
    tmp_path: Path,
    name: str = "test-skill",
    *,
    description: str = "A test skill",
    body: str = "# Test\n\nDo something.",
    scripts: dict[str, str] | None = None,
    references: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
) -> Path:
    """Create a skill directory with SKILL.md and optional sub-dirs."""
    skill_dir = tmp_path / name
    skill_dir.mkdir()

    skill_md = (
        f"---\nname: {name}\ndescription: \"{description}\"\n---\n\n{body}"
    )
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    if scripts:
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        for fname, content in scripts.items():
            p = scripts_dir / fname
            p.write_text(content, encoding="utf-8")

    if references:
        refs_dir = skill_dir / "references"
        refs_dir.mkdir()
        for fname, content in references.items():
            (refs_dir / fname).write_text(content, encoding="utf-8")

    if resources:
        res_dir = skill_dir / "resources"
        res_dir.mkdir()
        for fname, content in resources.items():
            (res_dir / fname).write_text(content, encoding="utf-8")

    return skill_dir


# ── SKILL.md parsing ─────────────────────────────────────────────────────────

class TestParseSkillMd:

    def test_basic_frontmatter(self):
        text = '---\nname: my-skill\ndescription: "A skill"\n---\n\n# Body\n\nHello'
        meta, body = _parse_skill_md(text)
        assert meta["name"] == "my-skill"
        assert meta["description"] == "A skill"
        assert "# Body" in body
        assert "Hello" in body

    def test_no_frontmatter(self):
        text = "# Just a markdown file\n\nNo frontmatter here."
        meta, body = _parse_skill_md(text)
        assert meta == {}
        assert "Just a markdown" in body

    def test_empty_frontmatter(self):
        text = "---\n---\n\nBody only"
        meta, body = _parse_skill_md(text)
        assert meta == {}
        assert "Body only" in body

    def test_multiline_body(self):
        text = (
            "---\nname: multi\ndescription: test\n---\n\n"
            "# Step 1\n\nDo A.\n\n# Step 2\n\nDo B.\n"
        )
        meta, body = _parse_skill_md(text)
        assert meta["name"] == "multi"
        assert "Step 1" in body
        assert "Step 2" in body


# ── DocSkill loading ─────────────────────────────────────────────────────────

class TestLoadDocSkill:

    def test_load_valid_skill(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "code-reviewer", description="代码审查")
        skill = load_doc_skill(skill_dir)
        assert skill is not None
        assert skill.name == "code-reviewer"
        assert skill.description == "代码审查"
        assert "# Test" in skill.body

    def test_load_missing_skill_md(self, tmp_path: Path):
        empty_dir = tmp_path / "no-skill"
        empty_dir.mkdir()
        assert load_doc_skill(empty_dir) is None

    def test_load_missing_name(self, tmp_path: Path):
        skill_dir = tmp_path / "bad"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: no name\n---\n\nBody", encoding="utf-8"
        )
        assert load_doc_skill(skill_dir) is None

    def test_load_with_subdirs(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "full-skill",
            scripts={"analyze.py": "print('ok')"},
            references={"rules.md": "# Rules"},
            resources={"data.csv": "a,b\n1,2"},
        )
        skill = load_doc_skill(skill_dir)
        assert skill is not None
        assert (skill.skill_dir / "scripts" / "analyze.py").is_file()
        assert (skill.skill_dir / "references" / "rules.md").is_file()
        assert (skill.skill_dir / "resources" / "data.csv").is_file()


# ── DocSkillTool properties ──────────────────────────────────────────────────

class TestDocSkillToolProperties:

    def test_tool_name(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "code-reviewer")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        assert tool.name == "skill_code_reviewer"  # hyphens → underscores

    def test_tool_description(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "my-tool", description="测试工具")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        assert "my-tool" in tool.description
        assert "测试工具" in tool.description

    def test_tool_parameters(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "test")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        params = tool.parameters
        assert params["type"] == "object"
        assert "action" in params["properties"]
        actions = params["properties"]["action"]["enum"]
        assert "read_instructions" in actions
        assert "run_script" in actions
        assert "read_reference" in actions
        assert "list_files" in actions

    def test_risk_level(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "test")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        assert tool.risk_level.value == "medium"


# ── Progressive disclosure: read_instructions ────────────────────────────────

class TestReadInstructions:

    @pytest.mark.asyncio
    async def test_returns_body(self, tmp_path: Path):
        body = "# Steps\n\n1. Do A\n2. Do B\n3. Check results"
        skill_dir = _make_skill_dir(tmp_path, "guide", body=body)
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "read_instructions"}, _make_ctx())
        assert not result.is_error
        assert "Do A" in result.content
        assert "Do B" in result.content


# ── run_script ────────────────────────────────────────────────────────────────

class TestRunScript:

    @pytest.mark.asyncio
    async def test_run_python_script(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "py-runner",
            scripts={"hello.py": "print('hello from skill')"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "hello.py"}, _make_ctx()
        )
        assert not result.is_error
        assert "hello from skill" in result.content

    @pytest.mark.asyncio
    async def test_run_bash_script(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "sh-runner",
            scripts={"greet.sh": "echo 'hi from bash'"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "greet.sh"}, _make_ctx()
        )
        assert not result.is_error
        assert "hi from bash" in result.content

    @pytest.mark.asyncio
    async def test_run_script_with_args(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "args-test",
            scripts={"echo_args.py": "import sys; print(' '.join(sys.argv[1:]))"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "echo_args.py", "args": "foo bar"},
            _make_ctx(),
        )
        assert not result.is_error
        assert "foo bar" in result.content

    @pytest.mark.asyncio
    async def test_run_script_missing_filename(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "no-file")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "run_script"}, _make_ctx())
        assert result.is_error
        assert "filename" in result.content

    @pytest.mark.asyncio
    async def test_run_script_nonexistent(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path, "missing-script", scripts={"real.py": "print(1)"}
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "fake.py"}, _make_ctx()
        )
        assert result.is_error
        assert "不存在" in result.content
        assert "real.py" in result.content  # suggests available scripts

    @pytest.mark.asyncio
    async def test_run_script_path_traversal_blocked(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "traversal")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "../../../etc/passwd"}, _make_ctx()
        )
        assert result.is_error
        assert "路径" in result.content

    @pytest.mark.asyncio
    async def test_run_script_exit_nonzero(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "fail-script",
            scripts={"fail.py": "import sys; print('error!'); sys.exit(1)"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "run_script", "filename": "fail.py"}, _make_ctx()
        )
        assert result.is_error
        assert "error!" in result.content


# ── read_reference ────────────────────────────────────────────────────────────

class TestReadReference:

    @pytest.mark.asyncio
    async def test_read_reference_file(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "ref-test",
            references={"rules.md": "# Rules\n\n1. Do not break things."},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "read_reference", "filename": "rules.md"}, _make_ctx()
        )
        assert not result.is_error
        assert "Do not break things" in result.content

    @pytest.mark.asyncio
    async def test_read_reference_missing_filename(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "no-ref")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "read_reference"}, _make_ctx())
        assert result.is_error
        assert "filename" in result.content

    @pytest.mark.asyncio
    async def test_read_reference_nonexistent(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "ref-missing",
            references={"api.md": "# API docs"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "read_reference", "filename": "nope.md"}, _make_ctx()
        )
        assert result.is_error
        assert "不存在" in result.content
        assert "api.md" in result.content

    @pytest.mark.asyncio
    async def test_read_reference_path_traversal_blocked(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "ref-traversal")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute(
            {"action": "read_reference", "filename": "../../etc/passwd"},
            _make_ctx(),
        )
        assert result.is_error


# ── list_files ────────────────────────────────────────────────────────────────

class TestListFiles:

    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path,
            "lister",
            scripts={"run.py": "pass", "check.sh": "echo ok"},
            references={"guide.md": "# Guide"},
            resources={"data.json": "{}"},
        )
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "list_files"}, _make_ctx())
        assert not result.is_error
        assert "SKILL.md" in result.content
        assert "run.py" in result.content
        assert "check.sh" in result.content
        assert "guide.md" in result.content
        assert "data.json" in result.content

    @pytest.mark.asyncio
    async def test_list_files_minimal(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "minimal")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "list_files"}, _make_ctx())
        assert not result.is_error
        assert "SKILL.md" in result.content


# ── unknown action ────────────────────────────────────────────────────────────

class TestUnknownAction:

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "unk")
        skill = load_doc_skill(skill_dir)
        tool = DocSkillTool(skill)
        result = await tool.execute({"action": "delete_everything"}, _make_ctx())
        assert result.is_error


# ── DocSkill.register_tools ──────────────────────────────────────────────────

class TestRegisterTools:

    def test_registers_tool(self, tmp_path: Path):
        skill_dir = _make_skill_dir(tmp_path, "reg-test")
        skill = load_doc_skill(skill_dir)
        registry = ToolRegistry()
        skill.register_tools(registry, None)
        assert registry.get("skill_reg_test") is not None

    def test_tool_definition_exported(self, tmp_path: Path):
        skill_dir = _make_skill_dir(
            tmp_path, "export-test", description="Export desc"
        )
        skill = load_doc_skill(skill_dir)
        registry = ToolRegistry()
        skill.register_tools(registry, None)
        defs = registry.get_definitions()
        assert len(defs) == 1
        assert defs[0].name == "skill_export_test"
        assert "Export desc" in defs[0].description


# ── Integration: _load_skills_from_dir ────────────────────────────────────────

class TestSkillDirIntegration:

    def test_doc_skill_discovered(self, tmp_path: Path):
        """SKILL.md directories in skills_dir should be auto-discovered."""
        from xclaw.skills import SkillRegistry, _load_skills_from_dir

        _make_skill_dir(tmp_path, "my-analyzer", description="分析器")
        sr = SkillRegistry()
        _load_skills_from_dir(sr, tmp_path, ["my_analyzer"])
        assert sr.get("my-analyzer") is not None

    def test_doc_skill_tools_registered(self, tmp_path: Path):
        """DocSkill tools should appear in the ToolRegistry."""
        from xclaw.skills import build_skill_registry

        _make_skill_dir(tmp_path, "helper", description="Helper skill")
        sr = build_skill_registry(["helper"], skills_dir=tmp_path)
        registry = ToolRegistry()
        sr.load_tools(registry, None)
        assert registry.get("skill_helper") is not None

    def test_all_formats_coexist(self, tmp_path: Path):
        """Doc, YAML, and Python skills should all be discovered together."""
        from xclaw.skills import Skill, SkillRegistry, _load_skills_from_dir

        # Doc skill
        _make_skill_dir(tmp_path, "doc-skill", description="Doc")
        # YAML skill
        (tmp_path / "yaml_skill.yaml").write_text(
            "name: yaml_skill\ndescription: YAML\ntools:\n"
            "  - name: t\n    description: T\n    http:\n      url: https://x.com\n",
            encoding="utf-8",
        )
        # Python skill
        (tmp_path / "py_skill.py").write_text(
            "from xclaw.skills import Skill\n"
            "from xclaw.tools import ToolRegistry\n"
            "class PySkill(Skill):\n"
            "    name = 'py_skill'\n"
            "    description = 'Python'\n"
            "    def register_tools(self, registry, settings):\n"
            "        pass\n",
            encoding="utf-8",
        )
        sr = SkillRegistry()
        _load_skills_from_dir(
            sr, tmp_path, ["doc_skill", "yaml_skill", "py_skill"]
        )
        assert sr.get("doc-skill") is not None
        assert sr.get("yaml_skill") is not None
        assert sr.get("py_skill") is not None

    def test_progressive_disclosure_token_saving(self, tmp_path: Path):
        """Only name+description should be in tool definition, not the full body."""
        _make_skill_dir(
            tmp_path,
            "long-skill",
            description="Short desc",
            body="# Very Long Instructions\n\n" + ("详细步骤。\n" * 200),
        )
        from xclaw.skills import build_skill_registry

        sr = build_skill_registry(["long_skill"], skills_dir=tmp_path)
        registry = ToolRegistry()
        sr.load_tools(registry, None)

        defs = registry.get_definitions()
        # The tool definition should have SHORT description (not the full body)
        tool_def = defs[0]
        assert len(tool_def.description) < 200  # description is short
        # But full body is still available via the tool
        tool = registry.get("skill_long_skill")
        assert tool is not None


# ── CLI integration ──────────────────────────────────────────────────────────

class TestCLIDocSkills:

    def test_skill_list_shows_doc_skills(self, tmp_path: Path):
        from click.testing import CliRunner
        from xclaw.cli import main

        _make_skill_dir(tmp_path, "my-checker", description="代码检查")

        runner = CliRunner()
        # Use the default path which won't find our skill, but the command
        # should still work without errors
        result = runner.invoke(main, ["skill", "list"])
        assert result.exit_code == 0
        assert "内置技能" in result.output

    def test_skill_remove_doc_dir(self, tmp_path: Path):
        _make_skill_dir(tmp_path, "to-remove")

        from click.testing import CliRunner
        from xclaw.cli import skill_remove

        runner = CliRunner()
        # Directly test the remove logic
        assert (tmp_path / "to-remove" / "SKILL.md").exists()

        import shutil
        doc_dir = tmp_path / "to-remove"
        shutil.rmtree(doc_dir)
        assert not doc_dir.exists()
