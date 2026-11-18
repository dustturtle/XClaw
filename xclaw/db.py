"""SQLite database layer using aiosqlite."""

from __future__ import annotations

import json
import hashlib
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS web_receipts (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    submission_id TEXT,
    run_id TEXT,
    baseline_cursor INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(scope,request_id)
);

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

CREATE TABLE IF NOT EXISTS powercore_sessions (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id),
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS powercore_runs (
    run_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    session_id TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    cursor INTEGER NOT NULL DEFAULT 0,
    input_request_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS powercore_submissions (
    submission_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    path TEXT NOT NULL,
    request_json TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL,
    conversational INTEGER NOT NULL,
    persist_message INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS powercore_pending_chat
ON powercore_submissions(chat_id) WHERE conversational=1;

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

CREATE TABLE IF NOT EXISTS investment_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    report_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    symbol_count INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER REFERENCES chats(id),
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    strategies_json TEXT NOT NULL,
    valuable_strategies_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS report_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER REFERENCES investment_reports(id),
    asset_type TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_links (
    link_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    public_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    max_uses INTEGER NULL,
    expires_at TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE IF NOT EXISTS invite_sessions (
    invite_session_id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    qrcode TEXT NOT NULL,
    qr_content TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bound_member_id TEXT NULL,
    superseded_by TEXT NULL,
    error TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (link_id) REFERENCES invite_links(link_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE IF NOT EXISTS tenant_members (
    member_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    ilink_user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_bound_at TEXT NOT NULL,
    UNIQUE (tenant_id, ilink_user_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE IF NOT EXISTS channel_credentials (
    credential_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    bot_token TEXT NOT NULL,
    ilink_bot_id TEXT NOT NULL,
    base_url TEXT NOT NULL,
    get_updates_buf TEXT NOT NULL,
    status TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    FOREIGN KEY (member_id) REFERENCES tenant_members(member_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE IF NOT EXISTS member_runtime_state (
    member_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    last_poll_at TEXT NULL,
    last_error TEXT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

CREATE TABLE IF NOT EXISTS message_dedup (
    tenant_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (credential_id, message_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
    FOREIGN KEY (credential_id) REFERENCES channel_credentials(credential_id)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        for table, additions in {
            "powercore_runs": {
                "wall_time_ms": "INTEGER NOT NULL DEFAULT 180000",
                "progress_json": "TEXT NOT NULL DEFAULT '{}'",
                "reply": "TEXT",
                "reply_cursor": "INTEGER",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "persist_message": "INTEGER NOT NULL DEFAULT 1",
                "conversational": "INTEGER NOT NULL DEFAULT 1",
                "submission_id": "TEXT",
                "observe_retry_at": "REAL",
                "observe_attempts": "INTEGER NOT NULL DEFAULT 0",
            },
            "powercore_submissions": {
                "receipt_id": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'run'",
                "run_id": "TEXT",
                "cursor": "INTEGER NOT NULL DEFAULT 0",
                "observe_retry_at": "REAL",
                "observe_attempts": "INTEGER NOT NULL DEFAULT 0",
            },
            "messages": {"execution_key": "TEXT", "response_model": "TEXT", "response_hash": "TEXT"},
            "web_receipts": {"requested_model": "TEXT"},
        }.items():
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row["name"] for row in await cur.fetchall()}
            for name, definition in additions.items():
                if name not in columns:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    if table == "powercore_runs" and name == "reply_cursor":
                        # Legacy replies are already in messages without execution keys.
                        # Do not replay all old completed chats during the first upgrade.
                        await self._conn.execute(
                            "UPDATE powercore_runs SET reply_cursor=cursor "
                            "WHERE status IN ('completed','failed','cancelled','waiting_input')"
                        )
                    if table == "powercore_runs" and name == "conversational":
                        await self._conn.execute(
                            "UPDATE powercore_runs SET conversational=0 WHERE NOT EXISTS "
                            "(SELECT 1 FROM powercore_sessions s WHERE s.run_id=powercore_runs.run_id)"
                        )
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS messages_execution_key ON messages(execution_key)"
        )
        await self._conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS web_receipt_submission_link
            AFTER INSERT ON powercore_submissions WHEN NEW.receipt_id IS NOT NULL
            BEGIN
                UPDATE web_receipts SET submission_id=NEW.submission_id,run_id=NEW.run_id,
                    baseline_cursor=NEW.cursor,updated_at=datetime('now')
                WHERE id=NEW.receipt_id AND submission_id IS NULL;
            END;
        """)
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

        await self.conn.execute(
            "INSERT INTO chats (channel, external_chat_id, chat_type, title) VALUES (?,?,?,?) "
            "ON CONFLICT(channel,external_chat_id) DO NOTHING",
            (channel, external_chat_id, chat_type, title),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM chats WHERE channel=? AND external_chat_id=?", (channel, external_chat_id),
        ) as cur:
            return (await cur.fetchone())["id"]

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM chats WHERE id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ── Messages ──────────────────────────────────────────────────────────────

    async def save_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        sender_name: str | None = None,
        *, response_model: str | None = None,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO messages (chat_id, role, content, sender_name,response_model,response_hash) VALUES (?,?,?,?,?,?)",
            (chat_id, role, content, sender_name, response_model,
             hashlib.sha256(content.encode()).hexdigest() if response_model else None),
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

    async def clear_session(self, chat_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM sessions WHERE chat_id = ?",
            (chat_id,),
        )
        await self.conn.commit()

    async def bind_powercore_run(
        self, chat_id: int, run_id: str, session_id: str, allowed_tools: list[str],
        *, conversational: bool = True, wall_time_ms: int = 180000, persist_message: bool = True,
        submission_id: str | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO powercore_runs(run_id,chat_id,session_id,allowed_tools_json,wall_time_ms,persist_message,conversational,submission_id) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING",
            (run_id, chat_id, session_id, json.dumps(allowed_tools), wall_time_ms,
             int(persist_message), int(conversational), submission_id),
        )
        if conversational:
            await self.conn.execute(
                "INSERT INTO powercore_sessions(chat_id,session_id,run_id) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id,run_id=excluded.run_id",
                (chat_id, session_id, run_id),
            )
        if submission_id:
            await self.conn.execute(
                "UPDATE web_receipts SET run_id=?,updated_at=datetime('now') WHERE submission_id=?",
                (run_id, submission_id),
            )
        await self.conn.commit()

    async def get_powercore_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM powercore_runs WHERE run_id=?", (run_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def save_powercore_submission(self, chat_id: int, path: str, request: dict,
                                       allowed_tools: list[str], *, conversational: bool, persist_message: bool,
                                       kind: str = "run", run_id: str | None = None, cursor: int = 0,
                                       submission_id: str | None = None) -> dict:
        submission_id = submission_id or request["idempotency_key"]
        from xclaw.receipts import current_receipt

        await self.conn.execute(
            "INSERT INTO powercore_submissions(submission_id,chat_id,path,request_json,allowed_tools_json,conversational,persist_message,kind,run_id,cursor,receipt_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (submission_id, chat_id, path, json.dumps(request, ensure_ascii=False),
             json.dumps(allowed_tools), int(conversational), int(persist_message), kind, run_id, cursor,
             current_receipt.get() if conversational else None),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT * FROM powercore_submissions WHERE submission_id=?", (submission_id,)) as cur:
            return dict(await cur.fetchone())

    async def get_pending_powercore_submission(self, chat_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM powercore_submissions WHERE chat_id=? AND conversational=1", (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def clear_powercore_submission(self, submission_id: str) -> None:
        await self.conn.execute("DELETE FROM powercore_submissions WHERE submission_id=?", (submission_id,))
        await self.conn.commit()

    async def pending_powercore_background(
        self, *, due_before: float | None = None, limit: int = 1000,
    ) -> tuple[list[dict], list[dict]]:
        due = "" if due_before is None else " AND observe_retry_at <= ?"
        params = (limit,) if due_before is None else (due_before, limit)
        async with self.conn.execute(
            "SELECT * FROM powercore_submissions WHERE conversational=0" + due +
            " ORDER BY COALESCE(observe_retry_at,0),created_at LIMIT ?", params,
        ) as cur:
            submissions = [dict(row) for row in await cur.fetchall()]
        async with self.conn.execute(
            "SELECT r.* FROM powercore_runs r WHERE r.conversational=0 AND NOT EXISTS "
            "(SELECT 1 FROM powercore_submissions s WHERE s.submission_id=r.submission_id) "
            "AND (r.status IN ('queued','running','waiting_input') OR "
            "(r.status IN ('completed','failed','cancelled') AND (r.reply_cursor IS NULL OR r.reply_cursor != r.cursor)))" +
            due + " ORDER BY COALESCE(observe_retry_at,0),created_at LIMIT ?", params,
        ) as cur:
            runs = [dict(row) for row in await cur.fetchall()]
        return submissions, runs

    async def arm_powercore_background(self) -> None:
        for table in ("powercore_runs", "powercore_submissions"):
            await self.conn.execute(
                f"UPDATE {table} SET observe_retry_at=0 WHERE conversational=0 AND observe_retry_at IS NULL"
            )
        await self.conn.commit()

    async def defer_powercore_observation(
        self, identity: str, retry_at: float, *, submission: bool = False,
    ) -> None:
        table, column = ("powercore_submissions", "submission_id") if submission else ("powercore_runs", "run_id")
        await self.conn.execute(
            f"UPDATE {table} SET observe_retry_at=?,observe_attempts=observe_attempts+1 WHERE {column}=?",
            (retry_at, identity),
        )
        await self.conn.commit()

    async def get_powercore_session(self, chat_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT r.* FROM powercore_sessions s JOIN powercore_runs r ON r.run_id=s.run_id "
            "WHERE s.chat_id=?", (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_powercore_run(
        self, run_id: str, status: str, cursor: int, input_request: dict | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE powercore_runs SET status=?,cursor=?,input_request_json=? WHERE run_id=?",
            (status, cursor, json.dumps(input_request) if input_request else None, run_id),
        )
        await self.conn.commit()

    async def clear_powercore_session(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM powercore_sessions WHERE chat_id=?", (chat_id,))
        await self.conn.commit()

    async def save_powercore_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE powercore_runs SET progress_json=? WHERE run_id=?",
            (json.dumps(progress, ensure_ascii=False), run_id),
        )
        await self.conn.commit()

    async def request_powercore_cancel(self, run_id: str) -> None:
        await self.conn.execute(
            "UPDATE powercore_runs SET cancel_requested=1,progress_json=? "
            "WHERE run_id=? AND status NOT IN ('completed','failed','cancelled')",
            (json.dumps({"stage": "cancelling", "message": "取消请求待确认"}), run_id),
        )
        await self.conn.commit()

    async def save_powercore_reply(self, run_id: str, reply: str, *, persist_message: bool = True) -> None:
        if persist_message:
            # A resumed observer can replay the terminal event, but not its chat message.
            await self.conn.execute(
                "INSERT INTO messages(chat_id,role,content,execution_key) "
                "SELECT chat_id,'assistant',?,'pc:'||run_id||':'||cursor FROM powercore_runs WHERE run_id=? "
                "ON CONFLICT(execution_key) DO NOTHING", (reply, run_id),
            )
        await self.conn.execute(
            "UPDATE powercore_runs SET reply=?,reply_cursor=cursor WHERE run_id=?", (reply, run_id),
        )
        await self.conn.commit()

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

    async def cancel_chat_task(self, chat_id: int, task_id: int) -> bool:
        async with self.conn.execute(
            "UPDATE scheduled_tasks SET status='cancelled' WHERE id=? AND chat_id=? AND status='active'",
            (task_id, chat_id),
        ) as cursor:
            cancelled = cursor.rowcount == 1
        await self.conn.commit()
        return cancelled

    async def get_scheduled_task(self, task_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

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

    # ── Investment reports ───────────────────────────────────────────────────

    async def add_investment_report(
        self,
        *,
        chat_id: int,
        report_type: str,
        title: str,
        summary: str,
        content_markdown: str,
        symbol_count: int,
        trigger_source: str,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO investment_reports "
            "(chat_id, report_type, title, summary, content_markdown, symbol_count, trigger_source) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                chat_id,
                report_type,
                title,
                summary,
                content_markdown,
                symbol_count,
                trigger_source,
            ),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_latest_investment_report(self, chat_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM investment_reports WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_investment_reports(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM investment_reports WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_investment_report(self, report_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM investment_reports WHERE id = ?",
            (report_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ── Strategy runs ────────────────────────────────────────────────────────

    async def add_strategy_run(
        self,
        *,
        chat_id: int,
        symbol: str,
        market: str,
        strategies: list[dict[str, Any]],
        valuable_strategies: list[dict[str, Any]],
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO strategy_runs "
            "(chat_id, symbol, market, strategies_json, valuable_strategies_json) "
            "VALUES (?,?,?,?,?)",
            (
                chat_id,
                symbol,
                market,
                json.dumps(strategies, ensure_ascii=False),
                json.dumps(valuable_strategies, ensure_ascii=False),
            ),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def list_strategy_runs(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM strategy_runs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["strategies"] = json.loads(item.pop("strategies_json"))
            item["valuable_strategies"] = json.loads(item.pop("valuable_strategies_json"))
            result.append(item)
        return result

    # ── Report exports ───────────────────────────────────────────────────────

    async def add_report_export(
        self,
        *,
        report_id: int,
        asset_type: str,
        mime_type: str,
        file_path: str,
        status: str,
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO report_exports (report_id, asset_type, mime_type, file_path, status) "
            "VALUES (?,?,?,?,?)",
            (report_id, asset_type, mime_type, file_path, status),
        ) as cur:
            await self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def list_report_exports(self, report_id: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM report_exports WHERE report_id = ? ORDER BY id ASC",
            (report_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def clear_report_exports(self, report_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM report_exports WHERE report_id = ?",
            (report_id,),
        )
        await self.conn.commit()

    # ── WeChat Multi-tenant Invite Flow ──────────────────────────────────────

    async def create_tenant(self, name: str) -> dict[str, Any]:
        tenant = {
            "tenant_id": str(uuid.uuid4()),
            "name": name.strip(),
            "status": "active",
            "created_at": _now_iso(),
        }
        await self.conn.execute(
            "INSERT INTO tenants (tenant_id, name, status, created_at) VALUES (?,?,?,?)",
            (
                tenant["tenant_id"],
                tenant["name"],
                tenant["status"],
                tenant["created_at"],
            ),
        )
        await self.conn.commit()
        return tenant

    async def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM tenants WHERE tenant_id = ?",
            (tenant_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def create_invite_link(
        self,
        tenant_id: str,
        *,
        max_uses: int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        link = {
            "link_id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "public_token": secrets.token_urlsafe(18),
            "status": "active",
            "max_uses": max_uses,
            "expires_at": expires_at,
            "created_at": _now_iso(),
        }
        await self.conn.execute(
            """
            INSERT INTO invite_links
            (link_id, tenant_id, public_token, status, max_uses, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link["link_id"],
                link["tenant_id"],
                link["public_token"],
                link["status"],
                link["max_uses"],
                link["expires_at"],
                link["created_at"],
            ),
        )
        await self.conn.commit()
        return link

    async def get_invite_link_by_token(self, public_token: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM invite_links WHERE public_token = ?",
            (public_token,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def disable_invite_link(self, link_id: str) -> None:
        await self.conn.execute(
            "UPDATE invite_links SET status = ? WHERE link_id = ?",
            ("disabled", link_id),
        )
        await self.conn.commit()

    async def create_invite_session(
        self,
        *,
        link_id: str,
        tenant_id: str,
        qrcode: str,
        qr_content: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        session = {
            "invite_session_id": str(uuid.uuid4()),
            "link_id": link_id,
            "tenant_id": tenant_id,
            "qrcode": qrcode,
            "qr_content": qr_content,
            "state": "pending",
            "expires_at": (datetime.now(timezone.utc).timestamp() + ttl_seconds),
            "bound_member_id": None,
            "superseded_by": None,
            "error": None,
            "created_at": _now_iso(),
        }
        expires_at = datetime.fromtimestamp(session["expires_at"], timezone.utc).isoformat()
        session["expires_at"] = expires_at
        await self.conn.execute(
            """
            INSERT INTO invite_sessions
            (invite_session_id, link_id, tenant_id, qrcode, qr_content, state, expires_at,
             bound_member_id, superseded_by, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["invite_session_id"],
                session["link_id"],
                session["tenant_id"],
                session["qrcode"],
                session["qr_content"],
                session["state"],
                session["expires_at"],
                session["bound_member_id"],
                session["superseded_by"],
                session["error"],
                session["created_at"],
            ),
        )
        await self.conn.commit()
        return session

    async def refresh_invite_session(
        self,
        invite_session_id: str,
        *,
        qrcode: str,
        qr_content: str,
        ttl_seconds: int,
    ) -> dict[str, Any] | None:
        current = await self.get_invite_session(invite_session_id)
        if current is None:
            return None
        new_session = await self.create_invite_session(
            link_id=current["link_id"],
            tenant_id=current["tenant_id"],
            qrcode=qrcode,
            qr_content=qr_content,
            ttl_seconds=ttl_seconds,
        )
        await self.update_invite_session_state(
            invite_session_id,
            "superseded",
            superseded_by=new_session["invite_session_id"],
        )
        return new_session

    async def get_invite_session(self, invite_session_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM invite_sessions WHERE invite_session_id = ?",
            (invite_session_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_invite_session_state(
        self,
        invite_session_id: str,
        state: str,
        *,
        error: str | None = None,
        bound_member_id: str | None = None,
        superseded_by: str | None = None,
    ) -> dict[str, Any] | None:
        await self.conn.execute(
            """
            UPDATE invite_sessions
            SET state = ?, error = COALESCE(?, error), bound_member_id = COALESCE(?, bound_member_id),
                superseded_by = COALESCE(?, superseded_by)
            WHERE invite_session_id = ?
            """,
            (
                state,
                error,
                bound_member_id,
                superseded_by,
                invite_session_id,
            ),
        )
        await self.conn.commit()
        return await self.get_invite_session(invite_session_id)

    async def bind_invite_session(
        self,
        invite_session_id: str,
        *,
        ilink_user_id: str,
        bot_token: str,
        ilink_bot_id: str,
        default_base_url: str,
        base_url: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        session = await self.get_invite_session(invite_session_id)
        if session is None:
            raise KeyError(f"Unknown invite session: {invite_session_id}")

        tenant_id = session["tenant_id"]
        now = _now_iso()
        resolved_base_url = base_url.strip() or default_base_url

        if not ilink_user_id.strip() or not bot_token.strip() or not ilink_bot_id.strip():
            raise ValueError("Confirmed invite session did not return complete iLink credentials.")

        async with self.conn.execute(
            """
            SELECT * FROM tenant_members
            WHERE tenant_id = ? AND ilink_user_id = ?
            """,
            (tenant_id, ilink_user_id.strip()),
        ) as cur:
            row = await cur.fetchone()

        if row:
            member_id = row["member_id"]
            await self.conn.execute(
                """
                UPDATE tenant_members
                SET last_bound_at = ?, status = ?
                WHERE member_id = ?
                """,
                (now, "active", member_id),
            )
        else:
            member_id = str(uuid.uuid4())
            await self.conn.execute(
                """
                INSERT INTO tenant_members
                (member_id, tenant_id, ilink_user_id, status, created_at, last_bound_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    tenant_id,
                    ilink_user_id.strip(),
                    "active",
                    now,
                    now,
                ),
            )

        await self.conn.execute(
            """
            UPDATE channel_credentials
            SET status = ?
            WHERE member_id = ? AND status = ?
            """,
            ("inactive", member_id, "active"),
        )

        credential_id = str(uuid.uuid4())
        await self.conn.execute(
            """
            INSERT INTO channel_credentials
            (credential_id, member_id, tenant_id, bot_token, ilink_bot_id, base_url,
             get_updates_buf, status, bound_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential_id,
                member_id,
                tenant_id,
                bot_token.strip(),
                ilink_bot_id.strip(),
                resolved_base_url,
                "",
                "active",
                now,
            ),
        )
        await self.conn.execute(
            """
            INSERT INTO member_runtime_state
            (member_id, tenant_id, context_token, last_poll_at, last_error)
            VALUES (?, ?, '', NULL, NULL)
            ON CONFLICT(member_id) DO NOTHING
            """,
            (member_id, tenant_id),
        )
        await self.conn.execute(
            """
            UPDATE invite_sessions
            SET state = ?, bound_member_id = ?
            WHERE invite_session_id = ?
            """,
            ("confirmed", member_id, invite_session_id),
        )
        await self.conn.commit()

        updated_session = await self.get_invite_session(invite_session_id)
        member = await self.get_member(member_id)
        credential = await self.get_credential(credential_id)
        if updated_session is None or member is None or credential is None:
            raise RuntimeError("Failed to persist invite binding state.")
        return updated_session, member, credential

    async def get_member(self, member_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM tenant_members WHERE member_id = ?",
            (member_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM channel_credentials WHERE credential_id = ?",
            (credential_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_active_credentials(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM channel_credentials WHERE status = ?",
            ("active",),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_active_credential_for_member(self, member_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            """
            SELECT * FROM channel_credentials
            WHERE member_id = ? AND status = ?
            ORDER BY bound_at DESC
            LIMIT 1
            """,
            (member_id, "active"),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_credential_get_updates_buf(
        self,
        credential_id: str,
        get_updates_buf: str,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE channel_credentials
            SET get_updates_buf = ?
            WHERE credential_id = ?
            """,
            (get_updates_buf, credential_id),
        )
        await self.conn.commit()

    async def get_runtime_state(
        self,
        member_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT * FROM member_runtime_state WHERE member_id = ?",
            (member_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            await self.conn.execute(
                """
                INSERT INTO member_runtime_state
                (member_id, tenant_id, context_token, last_poll_at, last_error)
                VALUES (?, ?, '', NULL, NULL)
                """,
                (member_id, tenant_id),
            )
            await self.conn.commit()
            async with self.conn.execute(
                "SELECT * FROM member_runtime_state WHERE member_id = ?",
                (member_id,),
            ) as cur:
                row = await cur.fetchone()

        if row is None:
            raise RuntimeError(f"Failed to load runtime state for member_id={member_id}")
        return dict(row)

    async def update_runtime_state(
        self,
        member_id: str,
        *,
        tenant_id: str,
        context_token: str | None = None,
        last_poll_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        current = await self.get_runtime_state(member_id, tenant_id)
        await self.conn.execute(
            """
            UPDATE member_runtime_state
            SET context_token = ?, last_poll_at = ?, last_error = ?
            WHERE member_id = ?
            """,
            (
                context_token if context_token is not None else current["context_token"],
                last_poll_at if last_poll_at is not None else current["last_poll_at"],
                last_error,
                member_id,
            ),
        )
        await self.conn.commit()

    async def has_seen_message(self, credential_id: str, message_id: str) -> bool:
        async with self.conn.execute(
            """
            SELECT 1 FROM message_dedup
            WHERE credential_id = ? AND message_id = ?
            """,
            (credential_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def remember_message(
        self,
        *,
        tenant_id: str,
        credential_id: str,
        message_id: str,
        keep_last: int = 200,
    ) -> None:
        now = _now_iso()
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO message_dedup
            (tenant_id, credential_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (tenant_id, credential_id, message_id, now),
        )
        await self.conn.execute(
            """
            DELETE FROM message_dedup
            WHERE rowid IN (
                SELECT rowid
                FROM message_dedup
                WHERE credential_id = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (credential_id, keep_last),
        )
        await self.conn.commit()

    async def get_tenant_summary(self, tenant_id: str) -> dict[str, Any] | None:
        tenant = await self.get_tenant(tenant_id)
        if tenant is None:
            return None

        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM invite_links WHERE tenant_id = ?",
            (tenant_id,),
        ) as cur:
            invite_link_count = (await cur.fetchone())["c"]
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM tenant_members WHERE tenant_id = ?",
            (tenant_id,),
        ) as cur:
            member_count = (await cur.fetchone())["c"]
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM channel_credentials
            WHERE tenant_id = ? AND status = ?
            """,
            (tenant_id, "active"),
        ) as cur:
            active_credential_count = (await cur.fetchone())["c"]

        return {
            "tenant_id": tenant["tenant_id"],
            "name": tenant["name"],
            "status": tenant["status"],
            "invite_link_count": invite_link_count,
            "member_count": member_count,
            "active_credential_count": active_credential_count,
        }

    async def list_tenant_members(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT
                m.member_id,
                m.tenant_id,
                m.ilink_user_id,
                m.status,
                c.ilink_bot_id AS current_ilink_bot_id,
                c.status AS credential_status,
                r.last_poll_at,
                r.last_error,
                m.created_at,
                m.last_bound_at
            FROM tenant_members m
            LEFT JOIN channel_credentials c
                ON c.member_id = m.member_id AND c.status = ?
            LEFT JOIN member_runtime_state r
                ON r.member_id = m.member_id
            WHERE m.tenant_id = ?
            ORDER BY m.created_at ASC
            """,
            ("active", tenant_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


@asynccontextmanager
async def get_db(db_path: str | Path) -> AsyncGenerator[Database, None]:
    """Async context manager that opens and closes a Database connection."""
    db = Database(db_path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
