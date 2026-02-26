"""SKILL.md directory-based skill definitions (Claude Agent Skills protocol).

Implements the Claude Agent Skills architecture standard where each skill
is a folder containing a ``SKILL.md`` file with YAML frontmatter and
Markdown instructions, plus optional ``scripts/``, ``references/``, and
``resources/`` directories.

Directory structure
-------------------

.. code-block:: text

    skill-name/
    ├── SKILL.md              # Core definition (required)
    ├── references/           # Reference docs (optional)
    │   ├── rules.md
    │   └── api-docs.md
    ├── scripts/              # Executable scripts (optional)
    │   ├── analyze.py
    │   └── validate.sh
    └── resources/            # Static data / assets (optional)

SKILL.md format
---------------

.. code-block:: markdown

    ---
    name: code-reviewer
    description: "代码审查助手"
    ---

    # Code Reviewer

    ## 执行步骤

    1. 读取目标文件
    2. 按照 references/rules.md 中的规范检查
    3. 运行 scripts/analyze.py 进行静态分析

Progressive Disclosure
~~~~~~~~~~~~~~~~~~~~~~
At startup only the YAML ``name`` and ``description`` are loaded into the
LLM context (minimal Token cost).  When the AI decides it needs a skill it
calls the registered tool to read the full SKILL.md instructions, run
scripts, or read reference documents on demand.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult, ToolRegistry


# ── SKILL.md frontmatter parser ───────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Parse a SKILL.md file into (frontmatter_dict, body_markdown).

    Returns ``({}, "")`` if the file cannot be parsed.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text  # no frontmatter – treat whole file as body
    try:
        meta = yaml.safe_load(m.group(1))
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, m.group(2)


# ── DocSkillTool ──────────────────────────────────────────────────────────────

class DocSkillTool(Tool):
    """Progressive-disclosure tool for a SKILL.md-based skill.

    Provides four actions:

    * ``read_instructions`` – returns the SKILL.md body (the core action)
    * ``run_script``        – executes a script from ``scripts/``
    * ``read_reference``    – reads a document from ``references/``
    * ``list_files``        – lists available scripts, references, and resources
    """

    _SCRIPT_TIMEOUT = 30  # seconds

    def __init__(self, skill: "DocSkill") -> None:
        self._skill = skill

    # ── Tool interface ────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        safe = self._skill.name.replace("-", "_")
        return f"skill_{safe}"

    @property
    def description(self) -> str:
        return (
            f"技能「{self._skill.name}」: {self._skill.description}。"
            "调用 read_instructions 获取详细执行步骤；"
            "run_script 运行辅助脚本；read_reference 查看参考文档。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "read_instructions",
                        "run_script",
                        "read_reference",
                        "list_files",
                    ],
                    "description": (
                        "操作类型: read_instructions=读取技能指令, "
                        "run_script=运行脚本, read_reference=查看参考文档, "
                        "list_files=列出技能目录文件"
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": "脚本或参考文档的文件名（run_script / read_reference 时需要）",
                },
                "args": {
                    "type": "string",
                    "description": "脚本参数（run_script 时可选，空格分隔）",
                },
            },
            "required": ["action"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        action = params.get("action", "read_instructions")
        if action == "read_instructions":
            return self._read_instructions()
        if action == "run_script":
            return await self._run_script(params)
        if action == "read_reference":
            return self._read_reference(params)
        if action == "list_files":
            return self._list_files()
        return ToolResult(content=f"未知操作: {action}", is_error=True)

    # ── action implementations ────────────────────────────────────────────

    def _read_instructions(self) -> ToolResult:
        return ToolResult(content=self._skill.body)

    async def _run_script(self, params: dict[str, Any]) -> ToolResult:
        filename = params.get("filename", "").strip()
        if not filename:
            return ToolResult(content="缺少 filename 参数", is_error=True)

        # Security: prevent path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return ToolResult(content="文件名不能包含路径分隔符", is_error=True)

        scripts_dir = self._skill.skill_dir / "scripts"
        script_path = scripts_dir / filename
        if not script_path.is_file():
            available = self._list_dir(scripts_dir)
            return ToolResult(
                content=f"脚本 '{filename}' 不存在。可用脚本: {available}",
                is_error=True,
            )

        # Determine how to run the script
        args_str = params.get("args", "").strip()
        cmd: list[str]
        if filename.endswith(".py"):
            cmd = ["python", str(script_path)]
        elif filename.endswith(".sh"):
            cmd = ["bash", str(script_path)]
        else:
            # Try to run directly (must be executable)
            cmd = [str(script_path)]

        if args_str:
            cmd.extend(args_str.split())

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._skill.skill_dir),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._SCRIPT_TIMEOUT
            )
            output = stdout.decode(errors="replace")
            err_output = stderr.decode(errors="replace")

            MAX_CHARS = 8000
            if len(output) > MAX_CHARS:
                output = output[:MAX_CHARS] + f"\n... [截断，已显示前 {MAX_CHARS} 字符]"

            result_text = output
            if err_output:
                result_text += f"\n[stderr]\n{err_output[:2000]}"
            if proc.returncode != 0:
                result_text += f"\n[exit code: {proc.returncode}]"

            return ToolResult(
                content=result_text or "(无输出)",
                is_error=proc.returncode != 0,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                content=f"脚本执行超时 ({self._SCRIPT_TIMEOUT}s)", is_error=True
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"脚本执行失败: {exc}", is_error=True)

    def _read_reference(self, params: dict[str, Any]) -> ToolResult:
        filename = params.get("filename", "").strip()
        if not filename:
            return ToolResult(content="缺少 filename 参数", is_error=True)

        # Security: prevent path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return ToolResult(content="文件名不能包含路径分隔符", is_error=True)

        refs_dir = self._skill.skill_dir / "references"
        ref_path = refs_dir / filename
        if not ref_path.is_file():
            available = self._list_dir(refs_dir)
            return ToolResult(
                content=f"参考文档 '{filename}' 不存在。可用文档: {available}",
                is_error=True,
            )

        try:
            text = ref_path.read_text(encoding="utf-8")
            MAX_CHARS = 8000
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + f"\n... [截断，已显示前 {MAX_CHARS} 字符]"
            return ToolResult(content=text)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"读取失败: {exc}", is_error=True)

    def _list_files(self) -> ToolResult:
        lines: list[str] = [f"技能「{self._skill.name}」文件列表:\n"]
        base = self._skill.skill_dir

        lines.append("📄 SKILL.md")

        for subdir_name in ("scripts", "references", "resources"):
            subdir = base / subdir_name
            if subdir.is_dir():
                files = sorted(f.name for f in subdir.iterdir() if f.is_file())
                if files:
                    lines.append(f"\n📁 {subdir_name}/")
                    for f in files:
                        lines.append(f"   {f}")

        return ToolResult(content="\n".join(lines))

    @staticmethod
    def _list_dir(d: Path) -> str:
        if not d.is_dir():
            return "(目录不存在)"
        files = sorted(f.name for f in d.iterdir() if f.is_file())
        return ", ".join(files) if files else "(空)"


# ── DocSkill ──────────────────────────────────────────────────────────────────

class DocSkill:
    """A skill defined by a SKILL.md directory structure.

    Implements the same duck-typed interface as :class:`~xclaw.skills.Skill`
    (``name``, ``description``, ``register_tools``) so it can be used
    interchangeably in the :class:`~xclaw.skills.SkillRegistry`.
    """

    def __init__(
        self,
        name: str,
        description: str,
        body: str,
        skill_dir: Path,
    ) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.skill_dir = skill_dir

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        """Register the progressive-disclosure tool for this skill."""
        tool = DocSkillTool(self)
        try:
            registry.register(tool)
        except ValueError:
            pass  # already registered


# ── Loader ────────────────────────────────────────────────────────────────────

def load_doc_skill(skill_dir: Path) -> DocSkill | None:
    """Load a :class:`DocSkill` from a directory containing ``SKILL.md``.

    Returns ``None`` if the directory is invalid.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None

    try:
        raw = skill_md.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Cannot read {skill_md}: {exc}")
        return None

    meta, body = _parse_skill_md(raw)
    name = meta.get("name", "")
    if not name:
        logger.warning(f"SKILL.md missing 'name' in frontmatter: {skill_md}")
        return None

    description = meta.get("description", "")

    return DocSkill(
        name=name,
        description=description,
        body=body,
        skill_dir=skill_dir,
    )
