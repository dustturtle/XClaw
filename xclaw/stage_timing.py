"""Bounded receipt-local timings. No model content, arguments or provider identifiers."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic_ns

active = ContextVar("stage_timing", default=None)
model_span = ContextVar("model_timing_span", default=None)


class StageTiming:
    def __init__(self, receipt):
        self.receipt = receipt
        self.start = monotonic_ns()
        self.events = []
        self.sequence = 0
        self.dropped = 0
        self.visible_round = None
        self.closed = False

    def mark(self, kind, *coordinates):
        if self.closed:
            return
        if len(self.events) >= 256:
            self.dropped += 1
            return
        self.events.append([kind, self.elapsed(), *coordinates])

    def elapsed(self):
        return (monotonic_ns() - self.start) // 1_000_000


@contextmanager
def span(kind, round_index, tool=None):
    timing = active.get()
    if timing is None or timing.closed:
        yield
        return
    timing.sequence += 1
    ident = timing.sequence
    timing.mark(kind + "_start", round_index, ident, *([tool] if tool is not None else []))
    token = model_span.set((timing, round_index, ident)) if kind == "model" else None
    status = ["ok"]
    try:
        yield status
    except BaseException as exc:
        status[0] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        raise
    finally:
        timing.mark(kind + "_end", round_index, ident, status[0])
        if token is not None:
            model_span.reset(token)


def first_delta(kind):
    model = model_span.get()
    if model:
        timing, round_index, ident = model
        timing.mark(kind, round_index, ident)


def visible(identity, round_index, kind, text):
    timing = active.get()
    if (timing and not timing.closed and timing.receipt == identity
            and timing.visible_round != round_index and text.strip()):
        timing.visible_round = round_index
        timing.mark("first_server_text", round_index, kind)
