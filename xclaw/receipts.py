"""Durable at-most-once web admission; never infer admission from the latest Run."""

import asyncio
from contextvars import ContextVar
import hashlib
import json
import uuid

from xclaw import stage_timing
from xclaw.lifecycle import LifecycleJournal, exception_fields
from xclaw.responses import RejectedReply, PresentationReply
from xclaw.text_stream import TextStreams, current_text_sink


current_receipt: ContextVar[str | None] = ContextVar("web_receipt", default=None)
REQUEST_ID_PATTERN = r"^[a-f0-9]{32}$"


class ReceiptConflict(Exception):
    pass


class ReceiptStore:
    def __init__(self, db):
        self.db = db
        self.boot_id = uuid.uuid4().hex
        self.journal = LifecycleJournal(db._path.parent / "logs", "chat-receipts")
        self.active: set[str] = set()
        self.text_streams = TextStreams()

    @staticmethod
    def scope(chat_id: str, token: str) -> str:
        return hashlib.sha256(json.dumps([chat_id, token]).encode()).hexdigest()

    async def lookup(self, scope, request_id):
        async with self.db.conn.execute(
            "SELECT * FROM web_receipts WHERE scope=? AND request_id=?", (scope, request_id)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def admit(self, scope, request_id, payload, *, allow_legacy=False):
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        identity = uuid.uuid4().hex
        async with self.db.conn.execute(
            "INSERT OR IGNORE INTO web_receipts(id,scope,request_id,fingerprint,boot_id,requested_model) VALUES(?,?,?,?,?,?)",
            (identity, scope, request_id, fingerprint, self.boot_id, payload.get("model")),
        ) as cur:
            created = cur.rowcount == 1
        await self.db.conn.commit()
        receipt = await self.lookup(scope, request_id)
        if receipt["fingerprint"] != fingerprint:
            legacy = {k:v for k,v in payload.items() if k != "model"}
            legacy_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
            if not (allow_legacy and receipt["requested_model"] is None and receipt["fingerprint"] == legacy_hash):
                raise ReceiptConflict()
        if created:
            self.active.add(identity)
            self.journal.emit("request_accepted", receipt_id=identity)
        else:
            self.journal.emit("request_deduplicated", receipt_id=receipt["id"])
        return receipt, created

    async def finish(self, identity, result):
        await self.db.conn.execute(
            "UPDATE web_receipts SET result_json=?,updated_at=datetime('now') WHERE id=? AND result_json IS NULL",
            (json.dumps(result, ensure_ascii=False), identity),
        )
        await self.db.conn.commit()

    async def execute(self, receipt, handler):
        identity = receipt["id"]
        token = current_receipt.set(identity)
        sink_token = current_text_sink.set(lambda event: self.text_streams.publish(identity, event))
        timing = stage_timing.StageTiming(identity)
        timing_token = stage_timing.active.set(timing)
        outcome = "failed"
        try:
            self.journal.emit("request_execution_started", receipt_id=identity)
            reply = await handler()
            if not isinstance(reply, str):
                raise TypeError("Invalid reply type")
            # Linked model work is completed only by its durable terminal record.
            fresh = await self.lookup(receipt["scope"], receipt["request_id"])
            if not fresh["submission_id"]:
                await self.finish(identity, {
                    "status": "failed" if isinstance(reply, RejectedReply) else "completed",
                    "reply": reply, "delivery": "direct",
                    **({"response_model":reply.response_model} if getattr(reply,"response_model",None) else {}),
                    **({"presentation_id":reply.presentation_id,"message_id":reply.message_id}
                       if isinstance(reply,PresentationReply) else {}),
                })
            else:
                projection = await self.project(fresh)
            outcome = (projection["status"] if fresh["submission_id"] else
                       "failed" if isinstance(reply, RejectedReply) else "completed")
            self.journal.emit("request_handler_returned", receipt_id=identity)
        except asyncio.CancelledError:
            outcome = "cancelled"
            self.journal.emit("request_interrupted", receipt_id=identity)
            raise
        except Exception as exc:
            self.journal.emit("request_handler_failed", receipt_id=identity, **exception_fields(exc))
            fresh = await self.lookup(receipt["scope"], receipt["request_id"])
            if not fresh["submission_id"]:
                await self.finish(identity, {
                    "status": "interrupted",
                    "message": "请求处理中断，可能已有部分操作生效；请核对后处理，不会自动重跑。",
                })
        finally:
            timing.closed = True
            stage_timing.active.reset(timing_token)
            self.journal.emit("request_stage_timing", receipt_id=identity,
                              elapsed_ms=timing.elapsed(), outcome=outcome,
                              stages=timing.events, dropped=timing.dropped)
            await self.text_streams.publish(identity, {"type":"terminal"})
            current_text_sink.reset(sink_token)
            current_receipt.reset(token)
            self.active.discard(identity)

    async def project(self, receipt, progress_handler=None):
        base = {"request_id": receipt["request_id"]}
        if receipt.get("requested_model"):
            base["model"] = receipt["requested_model"]
        if receipt["result_json"]:
            return {**base, **json.loads(receipt["result_json"])}
        if receipt["submission_id"]:
            # Resume observation/admission, not the business handler. Query again
            # after recovery because a lost admission response may now be bound.
            if progress_handler:
                await progress_handler()
            receipt = await self.lookup(receipt["scope"], receipt["request_id"])
            run = await self.db.get_powercore_run(receipt["run_id"]) if receipt["run_id"] else None
            if run:
                from xclaw.powercore import PowerCoreClient

                progress = PowerCoreClient._snapshot(run)
                advanced = run["cursor"] > receipt["baseline_cursor"]
                if advanced and progress.get("reply") is not None and run["status"] in {
                    "completed", "failed", "cancelled", "waiting_input"
                }:
                    result = {
                        "status": "completed" if run["status"] in {"completed", "waiting_input"} else "failed",
                        "reply": progress["reply"], "progress": progress,
                    }
                    await self.finish(receipt["id"], result)
                    self.journal.emit("request_terminal", receipt_id=receipt["id"], run_id=run["run_id"])
                    return {**base, **result}
                if advanced:
                    return {**base, "status": "processing", "progress": progress}
            async with self.db.conn.execute(
                "SELECT 1 FROM powercore_submissions WHERE submission_id=?", (receipt["submission_id"],)
            ) as cur:
                pending = await cur.fetchone()
            if pending or run or receipt["id"] in self.active:
                return {**base, "status": "processing", "message": "请求已接收，正在核对原任务。"}
        elif receipt["id"] in self.active or receipt["boot_id"] == self.boot_id:
            # The row is visible before admit() finishes dispatching its task.
            # Lack of an in-memory task in this boot is not evidence of a crash.
            return {**base, "status": "processing", "message": "请求已接收，正在准备执行。"}
        return {**base, "status": "interrupted",
                "message": "服务在回执落库后中断，执行结果尚未确认；不会自动重跑，请核对后处理。"}
