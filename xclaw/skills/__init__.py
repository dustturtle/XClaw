"""Skills system – extensible skill packs for XClaw.

A Skill is a named bundle of related tools that can be enabled/disabled
independently. Skills are discovered and loaded at startup; they register
their tools into the main ToolRegistry.

Built-in skills
---------------
* ``investment``      – stock analysis, watchlist, portfolio, market overview
* ``task_management`` – scheduled tasks
* ``memory``          – file + structured + semantic memory tools
* ``system``          – web search, web fetch, file tools, bash (if enabled)

Custom skills
-------------
Drop a Python file into ``<data_dir>/skills/`` that defines a subclass of
``Skill`` and lists it in ``enabled_skills`` in the config.

Example skill file (my_skill.py):

    from xclaw.skills import Skill
    from xclaw.tools import ToolRegistry

    class MySkill(Skill):
        name = "my_skill"
        description = "My custom skill pack"

        def register_tools(self, registry: ToolRegistry, settings) -> None:
            from my_package.tools import MyTool
            registry.register(MyTool())

Config:
    enabled_skills: [investment, task_management, memory, system, my_skill]
    skills_dir: "./xclaw.data/skills"   # optional custom skills directory
"""

from __future__ import annotations

import importlib.util
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from loguru import logger

from xclaw.tools import ToolRegistry


class Skill(ABC):
    """Abstract base class for XClaw skill packs."""

    #: Unique lowercase name used in config ``enabled_skills`` list.
    name: str = ""
    #: Human-readable description of what this skill provides.
    description: str = ""

    @abstractmethod
    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        """Register this skill's tools into *registry*.

        Called once at startup.  Should be idempotent – check whether a tool
        is already registered before calling ``registry.register()``.
        """
        ...


# ── Built-in skills ────────────────────────────────────────────────────────────

class InvestmentSkill(Skill):
    """Stock analysis, watchlist, portfolio management, and market overview."""

    name = "investment"
    description = "A股/美股/港股行情、技术指标、基本面、自选股、持仓管理、大盘概览"

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        from xclaw.tools.stock_quote import StockQuoteTool
        from xclaw.tools.stock_history import StockHistoryTool
        from xclaw.tools.stock_indicators import StockIndicatorsTool
        from xclaw.tools.stock_fundamentals import StockFundamentalsTool
        from xclaw.tools.stock_news import StockNewsTool
        from xclaw.tools.watchlist import WatchlistManageTool
        from xclaw.tools.portfolio import PortfolioManageTool
        from xclaw.tools.market_overview import MarketOverviewTool
        from xclaw.tools.stock_backtest import StockBacktestTool

        for tool_cls in (
            StockQuoteTool,
            StockHistoryTool,
            StockIndicatorsTool,
            StockFundamentalsTool,
            StockNewsTool,
            WatchlistManageTool,
            PortfolioManageTool,
            MarketOverviewTool,
            StockBacktestTool,
        ):
            _safe_register(registry, tool_cls())


class TaskManagementSkill(Skill):
    """Scheduled task creation, listing, and cancellation."""

    name = "task_management"
    description = "定时任务创建、查询、取消（基于 APScheduler）"

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        from xclaw.tools.schedule import (
            ScheduleTaskTool,
            ListScheduledTasksTool,
            CancelScheduledTaskTool,
        )
        for tool_cls in (ScheduleTaskTool, ListScheduledTasksTool, CancelScheduledTaskTool):
            _safe_register(registry, tool_cls())


class MemorySkill(Skill):
    """File memory, structured memory, and semantic memory search."""

    name = "memory"
    description = "AGENTS.md 文件记忆、结构化记忆、语义搜索"

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        from xclaw.tools.memory_tools import (
            ReadMemoryTool,
            WriteMemoryTool,
            StructuredMemoryReadTool,
            StructuredMemoryUpdateTool,
            SemanticMemorySearchTool,
        )
        for tool_cls in (
            ReadMemoryTool,
            WriteMemoryTool,
            StructuredMemoryReadTool,
            StructuredMemoryUpdateTool,
            SemanticMemorySearchTool,
        ):
            _safe_register(registry, tool_cls())


class SystemSkill(Skill):
    """Web search, web fetch, file I/O, optional bash, and sub-agent."""

    name = "system"
    description = "网页搜索、网页抓取、文件读写、Bash（可选）、子 Agent"

    def register_tools(self, registry: ToolRegistry, settings: Any) -> None:
        from xclaw.tools.web_search import WebSearchTool
        from xclaw.tools.web_fetch import WebFetchTool
        from xclaw.tools.file_tools import ReadFileTool, WriteFileTool

        for tool_cls in (WebSearchTool, WebFetchTool, ReadFileTool, WriteFileTool):
            _safe_register(registry, tool_cls())

        bash_enabled = getattr(settings, "bash_enabled", False) if settings else False
        if bash_enabled:
            from xclaw.tools.bash_tool import BashTool
            _safe_register(registry, BashTool())


# Registry of all built-in skills keyed by skill.name
_BUILTIN_SKILLS: dict[str, Skill] = {
    s.name: s
    for s in [InvestmentSkill(), TaskManagementSkill(), MemorySkill(), SystemSkill()]
}

_ALL_BUILTIN_NAMES = list(_BUILTIN_SKILLS.keys())


def _safe_register(registry: ToolRegistry, tool: Any) -> None:
    """Register a tool, silently skipping if already registered."""
    try:
        registry.register(tool)
    except ValueError:
        pass  # already registered


# ── SkillRegistry ──────────────────────────────────────────────────────────────

class SkillRegistry:
    """Loads and manages skill packs, then populates the ToolRegistry."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' is already registered.")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def load_tools(self, registry: ToolRegistry, settings: Any) -> None:
        """Register all loaded skills' tools into *registry*."""
        for skill in self._skills.values():
            try:
                skill.register_tools(registry, settings)
                logger.debug(f"Skill '{skill.name}' loaded")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Skill '{skill.name}' failed to load tools: {exc}")


def build_skill_registry(
    enabled_skills: list[str] | None,
    skills_dir: str | Path | None = None,
) -> SkillRegistry:
    """Construct a SkillRegistry from the enabled skill names.

    Args:
        enabled_skills: List of skill names to enable.  Pass ``None`` or
                        ``["all"]`` to enable all built-in skills.
        skills_dir:     Optional directory to load custom skills from.

    Returns:
        Populated SkillRegistry ready for ``load_tools()``.
    """
    sr = SkillRegistry()

    # Determine which built-in skills to activate
    if enabled_skills is None or enabled_skills == ["all"] or enabled_skills == []:
        names_to_load = _ALL_BUILTIN_NAMES
    else:
        names_to_load = [n.lower().strip() for n in enabled_skills]

    for name in names_to_load:
        skill = _BUILTIN_SKILLS.get(name)
        if skill is not None:
            sr.register(skill)
        else:
            logger.warning(f"Unknown built-in skill '{name}', will look in skills_dir")

    # Load custom skills from skills_dir
    if skills_dir:
        _load_skills_from_dir(sr, Path(skills_dir), names_to_load)

    return sr


def _load_skills_from_dir(
    registry: SkillRegistry,
    skills_dir: Path,
    requested_names: list[str],
) -> None:
    """Scan *skills_dir* for skill definitions.

    Supports three formats (checked in this order):

    1. **SKILL.md directories** – sub-directories containing a ``SKILL.md``
       file (Claude Agent Skills protocol).
    2. **YAML skill files** – ``.yaml`` files with declarative tool defs.
    3. **Python skill files** – ``.py`` files with ``Skill`` subclasses.
    """
    if not skills_dir.is_dir():
        return

    # ── SKILL.md directories (Claude Agent Skills protocol) ───────────────
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        # Check if explicitly requested (or "all")
        dir_name = entry.name.replace("-", "_")
        if requested_names != _ALL_BUILTIN_NAMES and dir_name not in requested_names:
            continue
        from xclaw.skills.doc_skill import load_doc_skill

        skill = load_doc_skill(entry)
        if skill is not None:
            try:
                registry.register(skill)
                logger.info(f"Doc skill '{skill.name}' loaded from {entry}")
            except ValueError:
                pass

    # ── YAML skill files ──────────────────────────────────────────────────
    for yaml_file in sorted(skills_dir.glob("*.yaml")):
        stem = yaml_file.stem
        if stem.startswith("_"):
            continue
        if requested_names != _ALL_BUILTIN_NAMES and stem not in requested_names:
            continue
        from xclaw.skills.yaml_skill import load_yaml_skill

        skill = load_yaml_skill(yaml_file)
        if skill is not None:
            try:
                registry.register(skill)
                logger.info(f"YAML skill '{skill.name}' loaded from {yaml_file}")
            except ValueError:
                pass

    # ── Python skill files ────────────────────────────────────────────────
    for py_file in sorted(skills_dir.glob("*.py")):
        stem = py_file.stem
        if stem.startswith("_"):
            continue
        # Only load if explicitly requested (or if "all" was requested)
        if requested_names != _ALL_BUILTIN_NAMES and stem not in requested_names:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"xclaw_skill_{stem}", py_file)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"xclaw_skill_{stem}"] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            # Find Skill subclasses in the module
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Skill)
                    and attr is not Skill
                    and attr.name
                ):
                    skill_instance = attr()
                    try:
                        registry.register(skill_instance)
                        logger.info(f"Custom skill '{skill_instance.name}' loaded from {py_file}")
                    except ValueError:
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to load skill from {py_file}: {exc}")
