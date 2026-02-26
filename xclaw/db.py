"""SQLite database layer using aiosqlite."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    chat_type TEXT DEFAULT 'private',
    title TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(channel, external_chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sender_name TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id),
    messages_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    description TEXT NOT NULL,
    cron_expression TEXT,
    prompt TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    next_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    content TEXT NOT NULL,
    category TEXT,
    confidence REAL DEFAULT 0.8,
    source TEXT DEFAULT 'explicit',
    is_archived INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    symbol TEXT NOT NULL,
    market TEXT DEFAULT 'CN',
    name TEXT,
    notes TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id, symbol, market)
);

CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    symbol TEXT NOT NULL,
    market TEXT DEFAULT 'CN',
    shares REAL NOT NULL,
    avg_cost REAL NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id, symbol, market)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class Database:
    """Async SQLite wrapper providing typed CRUD helpers."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call await db.connect() first.")
        return self._conn

    # ── Chats ─────────────────────────────────────────────────────────────────

    async def get_or_create_chat(
        self,
        channel: str,
        external_chat_id: str,
        chat_type: str = "private",
        title: str | None = None,
    ) -> int:
        """Return the internal chat.id, creating the row if it doesn't exist."""
        async with self.conn.execute(
            "SELECT id FROM chats WHERE channel = ? AND external_chat_id = ?",
            (channel, external_chat_id),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["id"]

        async with self.conn.execute(
            "INSERT INTO chats (channel, external_chat_id, chat_type, title) VALUES (?,?,?,?)",
            (channel, external_chat_id, chat_type, title),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    # ── Messages ──────────────────────────────────────────────────────────────

    async def save_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        sender_name: str | None = None,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO messages (chat_id, role, content, sender_name) VALUES (?,?,?,?)",
            (chat_id, role, content, sender_name),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_recent_messages(self, chat_id: int, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT role, content, sender_name, created_at FROM messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def save_session(self, chat_id: int, messages: list[Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "INSERT OR REPLACE INTO sessions (chat_id, messages_json, updated_at) VALUES (?,?,?)",
            (chat_id, json.dumps(messages, ensure_ascii=False), now),
        )
        await self.conn.commit()

    async def load_session(self, chat_id: int) -> list[Any] | None:
        async with self.conn.execute(
            "SELECT messages_json FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return json.loads(row["messages_json"])

    # ── Memories ──────────────────────────────────────────────────────────────

    async def add_memory(
        self,
        chat_id: int,
        content: str,
        category: str | None = None,
        confidence: float = 0.8,
        source: str = "explicit",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self.conn.execute(
            "INSERT INTO memories (chat_id, content, category, confidence, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (chat_id, content, category, confidence, source, now, now),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_memories(self, chat_id: int, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM memories WHERE chat_id = ?"
        params: list[Any] = [chat_id]
        if not include_archived:
            query += " AND is_archived = 0"
        query += " ORDER BY confidence DESC"
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def archive_memory(self, memory_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "UPDATE memories SET is_archived = 1, updated_at = ? WHERE id = ?",
            (now, memory_id),
        )
        await self.conn.commit()

    # ── Scheduled tasks ───────────────────────────────────────────────────────

    async def add_scheduled_task(
        self,
        chat_id: int,
        description: str,
        prompt: str,
        cron_expression: str | None = None,
        next_run_at: str | None = None,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO scheduled_tasks (chat_id, description, cron_expression, prompt, next_run_at) "
            "VALUES (?,?,?,?,?)",
            (chat_id, description, cron_expression, prompt, next_run_at),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_active_tasks(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE status = 'active' ORDER BY next_run_at",
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_task_status(self, task_id: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE scheduled_tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
        await self.conn.commit()

    # ── Watchlist ─────────────────────────────────────────────────────────────

    async def add_to_watchlist(
        self,
        chat_id: int,
        symbol: str,
        market: str = "CN",
        name: str | None = None,
        notes: str | None = None,
    ) -> int:
        async with self.conn.execute(
            "INSERT OR REPLACE INTO watchlist (chat_id, symbol, market, name, notes) VALUES (?,?,?,?,?)",
            (chat_id, symbol, market, name, notes),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def remove_from_watchlist(self, chat_id: int, symbol: str, market: str = "CN") -> bool:
        async with self.conn.execute(
            "DELETE FROM watchlist WHERE chat_id = ? AND symbol = ? AND market = ?",
            (chat_id, symbol, market),
        ) as cur:
            await self.conn.commit()
            return cur.rowcount > 0  # type: ignore[return-value]

    async def get_watchlist(self, chat_id: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM watchlist WHERE chat_id = ? ORDER BY added_at",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def upsert_portfolio(
        self,
        chat_id: int,
        symbol: str,
        market: str,
        shares: float,
        avg_cost: float,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self.conn.execute(
            "INSERT INTO portfolio (chat_id, symbol, market, shares, avg_cost, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, symbol, market) DO UPDATE SET "
            "shares=excluded.shares, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at",
            (chat_id, symbol, market, shares, avg_cost, now),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_portfolio(self, chat_id: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM portfolio WHERE chat_id = ? ORDER BY updated_at DESC",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def remove_from_portfolio(self, chat_id: int, symbol: str, market: str = "CN") -> bool:
        async with self.conn.execute(
            "DELETE FROM portfolio WHERE chat_id = ? AND symbol = ? AND market = ?",
            (chat_id, symbol, market),
        ) as cur:
            await self.conn.commit()
            return cur.rowcount > 0  # type: ignore[return-value]

    # ── LLM Usage ─────────────────────────────────────────────────────────────

    async def record_usage(
        self,
        chat_id: int | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO llm_usage (chat_id, model, input_tokens, output_tokens) VALUES (?,?,?,?)",
            (chat_id, model, input_tokens, output_tokens),
        )
        await self.conn.commit()


@asynccontextmanager
async def get_db(db_path: str | Path) -> AsyncGenerator[Database, None]:
    """Async context manager that opens and closes a Database connection."""
    db = Database(db_path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
