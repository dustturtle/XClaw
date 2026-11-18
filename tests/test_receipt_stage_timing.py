import asyncio
import json

import pytest

from xclaw import stage_timing as st
from xclaw.receipts import ReceiptStore
from xclaw.text_stream import current_text_sink


@pytest.mark.asyncio
async def test_server_text_is_parsed_nonempty_and_scoped_to_receipt(db):
    store = ReceiptStore(db)

    async def run(key):
        receipt, _ = await store.admit(key, key * 32, {"message": "private prompt"})

        async def handle():
            send = current_text_sink.get()
            for i in range(2):
                with st.span("model", i):
                    await send({"type": "reset", "round": i})
                    st.first_delta("first_text_delta")
                    await send({"type":"ui_snapshot", "round":i, "presentation":{}, "text":" "})
                    await send({"type":"ui_snapshot", "round":i, "presentation":{}, "text":"private answer"})
                    await asyncio.sleep(0)
                    await send({"type":"ui_snapshot", "round":i, "presentation":{}, "text":"still private"})
            return "ok"
        await store.execute(receipt, handle)
        return receipt["id"]

    ids = await asyncio.gather(run("a"), run("b"))
    rows = [json.loads(line) for line in store.journal.path.read_text().splitlines()]
    rows = [row for row in rows if row["event"] == "request_stage_timing"]
    assert {r["receipt_id"] for r in rows} == set(ids)
    for row in rows:
        assert row["outcome"] == "completed"
        stages = row["stages"]
        assert [(e[2], e[3]) for e in stages if e[0] == "first_server_text"] == [(0, "ui_snapshot"), (1, "ui_snapshot")]
        assert [e[1] for e in stages] == sorted(e[1] for e in stages)
        assert "private" not in json.dumps(row)


def test_cap_and_cancelled_spans():
    trace = st.StageTiming("id")
    token = st.active.set(trace)
    try:
        with pytest.raises(asyncio.CancelledError):
            with st.span("tool", 3, "lookup"):
                raise asyncio.CancelledError
        assert trace.events[-1][0::4] == ["tool_end", "cancelled"]
        for _ in range(300):
            trace.mark("x")
        assert len(trace.events) == 256 and trace.dropped == 46
    finally:
        st.active.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ui_commit", "draft_failed"])
async def test_first_visible_terminal_prefix_records_round_and_never_empty(kind):
    from xclaw.text_stream import TextStreams
    streams = TextStreams()
    timing = st.StageTiming("r")
    token = st.active.set(timing)
    try:
        for i, text in enumerate(["", " ", "parsed prefix"]):
            await streams.publish("r", {"type":"reset", "round":i})
            await streams.publish("r", {"type":kind, "round":i, "text":text, "presentation":{}})
        assert [(e[0], *e[2:]) for e in timing.events] == [("first_server_text", 2, kind)]
    finally:
        st.active.reset(token)
