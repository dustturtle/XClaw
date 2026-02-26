"""File-based memory management (AGENTS.md) and structured memory helpers."""

from __future__ import annotations

from pathlib import Path

from xclaw.db import Database


class FileMemory:
    """Manages per-chat AGENTS.md files for long-term free-form notes."""

    def __init__(self, groups_path: Path) -> None:
        self.groups_path = groups_path

    def _memory_file(self, chat_id: int | str) -> Path:
        return self.groups_path / str(chat_id) / "AGENTS.md"

    def read(self, chat_id: int | str) -> str:
        """Read the memory file for a chat; returns empty string if not found."""
        path = self._memory_file(chat_id)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write(self, chat_id: int | str, content: str) -> None:
        """Write/overwrite the memory file for a chat."""
        path = self._memory_file(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append(self, chat_id: int | str, note: str) -> None:
        """Append a note to the memory file."""
        existing = self.read(chat_id)
        new_content = (existing.rstrip() + "\n\n" + note.strip()) if existing else note.strip()
        self.write(chat_id, new_content)


class StructuredMemory:
    """Provides access to the structured memories table with basic deduplication."""

    JACCARD_THRESHOLD = 0.7  # Similarity threshold for deduplication

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Simple word-level tokenizer for Jaccard similarity."""
        return set(text.lower().split())

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a and not b:
            return 1.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    async def add(
        self,
        chat_id: int,
        content: str,
        category: str | None = None,
        confidence: float = 0.8,
        source: str = "explicit",
    ) -> int | None:
        """Add a memory, skipping if a near-duplicate already exists.

        Returns the new memory id, or None if skipped as duplicate.
        """
        existing = await self.db.get_memories(chat_id)
        tokens_new = self._tokenize(content)
        for mem in existing:
            tokens_existing = self._tokenize(mem["content"])
            if self._jaccard(tokens_new, tokens_existing) >= self.JACCARD_THRESHOLD:
                return None  # skip duplicate
        return await self.db.add_memory(chat_id, content, category, confidence, source)

    async def get_all(self, chat_id: int) -> list[dict]:
        return await self.db.get_memories(chat_id)

    async def archive(self, memory_id: int) -> None:
        await self.db.archive_memory(memory_id)

    def format_for_prompt(self, memories: list[dict]) -> str:
        """Format memories for injection into a system prompt."""
        if not memories:
            return ""
        lines = ["## 记忆 (Memories)"]
        for mem in memories:
            cat = f"[{mem['category']}] " if mem.get("category") else ""
            lines.append(f"- {cat}{mem['content']}")
        return "\n".join(lines)
