"""File-based memory management (AGENTS.md) and structured memory helpers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

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


# ── Semantic similarity helpers ────────────────────────────────────────────────

def _extract_bigrams(text: str) -> dict[str, int]:
    """Extract character bi-grams from text as a frequency map.

    Character bi-grams work well for both Chinese and Latin text without
    requiring a tokeniser or any third-party library.
    """
    text = text.lower()
    freq: dict[str, int] = {}
    for i in range(len(text) - 1):
        bg = text[i : i + 2]
        freq[bg] = freq.get(bg, 0) + 1
    return freq


def _to_unit_vector(freq: dict[str, int]) -> dict[str, float]:
    """Normalise a frequency map to a unit vector."""
    norm = math.sqrt(sum(v * v for v in freq.values()))
    if norm == 0:
        return {}
    return {k: v / norm for k, v in freq.items()}


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Compute cosine similarity between two sparse unit vectors."""
    if not a or not b:
        return 0.0
    # Dot product of unit vectors = cosine similarity
    return sum(a.get(k, 0.0) * v for k, v in b.items())


class StructuredMemory:
    """Provides access to the structured memories table with basic deduplication."""

    JACCARD_THRESHOLD = 0.7  # Similarity threshold for deduplication
    # Minimum cosine similarity (0–1) for a memory to appear in semantic_search results.
    # 0.3 balances recall (find loosely related memories) vs. precision (avoid noise).
    # Lower → more results, higher → only near-exact matches.
    SEMANTIC_THRESHOLD = 0.3  # Cosine similarity threshold for semantic search

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

    async def semantic_search(
        self,
        chat_id: int,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search memories by semantic similarity using character bi-gram cosine similarity.

        Returns up to *top_k* memories sorted by descending similarity score.
        Each returned dict includes all memory fields plus a ``score`` key (0–1).
        """
        memories = await self.db.get_memories(chat_id)
        if not memories:
            return []

        query_vec = _to_unit_vector(_extract_bigrams(query))
        if not query_vec:
            return memories[:top_k]

        scored: list[tuple[float, dict[str, Any]]] = []
        for mem in memories:
            mem_vec = _to_unit_vector(_extract_bigrams(mem["content"]))
            score = _cosine_similarity(query_vec, mem_vec)
            if score >= self.SEMANTIC_THRESHOLD:
                scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, mem in scored[:top_k]:
            entry = dict(mem)
            entry["score"] = round(score, 4)
            results.append(entry)
        return results

    def format_for_prompt(self, memories: list[dict]) -> str:
        """Format memories for injection into a system prompt."""
        if not memories:
            return ""
        lines = ["## 记忆 (Memories)"]
        for mem in memories:
            cat = f"[{mem['category']}] " if mem.get("category") else ""
            lines.append(f"- {cat}{mem['content']}")
        return "\n".join(lines)

