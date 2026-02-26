"""Tests for the memory module (FileMemory and StructuredMemory)."""

from __future__ import annotations

from pathlib import Path
import pytest

from xclaw.memory import FileMemory, StructuredMemory


# ── FileMemory ────────────────────────────────────────────────────────────────

def test_file_memory_read_empty(tmp_path: Path):
    fm = FileMemory(tmp_path / "groups")
    assert fm.read(42) == ""


def test_file_memory_write_and_read(tmp_path: Path):
    fm = FileMemory(tmp_path / "groups")
    fm.write(1, "# Memory\n- 偏好价值投资")
    content = fm.read(1)
    assert "价值投资" in content


def test_file_memory_append(tmp_path: Path):
    fm = FileMemory(tmp_path / "groups")
    fm.write(1, "# Note 1")
    fm.append(1, "Note 2")
    content = fm.read(1)
    assert "Note 1" in content
    assert "Note 2" in content


def test_file_memory_write_creates_directories(tmp_path: Path):
    fm = FileMemory(tmp_path / "deep" / "nested" / "groups")
    fm.write(99, "Hello")
    assert (tmp_path / "deep" / "nested" / "groups" / "99" / "AGENTS.md").exists()


# ── StructuredMemory ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_memory_add_and_get(db):
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_user")
    mid = await sm.add(chat_id, "用户偏好价值投资", category="偏好")
    assert mid is not None

    memories = await sm.get_all(chat_id)
    assert len(memories) == 1
    assert memories[0]["content"] == "用户偏好价值投资"


@pytest.mark.asyncio
async def test_structured_memory_deduplication(db):
    """Near-duplicate memories should be skipped."""
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_dedup")
    await sm.add(chat_id, "用户偏好价值投资风格", category="偏好")
    # Very similar – should be deduplicated
    mid2 = await sm.add(chat_id, "用户偏好价值投资风格", category="偏好")
    assert mid2 is None  # skipped
    memories = await sm.get_all(chat_id)
    assert len(memories) == 1


@pytest.mark.asyncio
async def test_structured_memory_different_content_not_deduplicated(db):
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_diff")
    await sm.add(chat_id, "用户偏好价值投资", category="偏好")
    await sm.add(chat_id, "用户关注新能源板块", category="关注")
    memories = await sm.get_all(chat_id)
    assert len(memories) == 2


@pytest.mark.asyncio
async def test_structured_memory_format_for_prompt(db):
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_format")
    await sm.add(chat_id, "用户偏好价值投资", category="偏好")
    memories = await sm.get_all(chat_id)
    prompt = sm.format_for_prompt(memories)
    assert "偏好" in prompt
    assert "价值投资" in prompt


@pytest.mark.asyncio
async def test_structured_memory_archive(db):
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_archive")
    mid = await sm.add(chat_id, "旧的记忆")
    await sm.archive(mid)
    memories = await sm.get_all(chat_id)
    assert len(memories) == 0


@pytest.mark.asyncio
async def test_structured_memory_jaccard_threshold(db):
    """Entries that are different enough should both be stored."""
    sm = StructuredMemory(db)
    chat_id = await db.get_or_create_chat("web", "sm_jaccard")
    await sm.add(chat_id, "苹果公司股价上涨")
    await sm.add(chat_id, "特斯拉汽车销量增加")
    memories = await sm.get_all(chat_id)
    assert len(memories) == 2
