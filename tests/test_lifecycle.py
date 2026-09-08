import json
import multiprocessing
import os
from pathlib import Path
import signal
import time

import pytest

from scripts.run_powercore import monitor_runtime
from xclaw.lifecycle import LifecycleJournal, exception_fields


def die_by_signal():
    os.kill(os.getpid(), signal.SIGKILL)


def test_parent_records_actual_sigkill_exit(tmp_path):
    process = multiprocessing.get_context("spawn").Process(target=die_by_signal)
    process.start()
    journal = LifecycleJournal(tmp_path, "test-parent")
    code = monitor_runtime(process, [], journal, 1)
    process.join(timeout=5)
    assert code == -signal.SIGKILL
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    event = next(e for e in events if e["event"] == "child_exited")
    assert event["signal"] == 9
    assert event["child_pid"] == process.pid
    assert event["exit_code"] == -9


def test_journal_only_accepts_safe_fields(tmp_path):
    journal = LifecycleJournal(tmp_path, "test")
    journal.emit("test_event", exception_type="ValueError", api_key="secret", prompt="private")
    text = journal.path.read_text()
    assert "secret" not in text and "private" not in text
    assert json.loads(text)["exception_type"] == "ValueError"
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_exception_frames_keep_locations_without_messages_or_locals(tmp_path):
    journal = LifecycleJournal(tmp_path, "test")
    try:
        raise RuntimeError("private-api-key")
    except RuntimeError as error:
        journal.emit("failure", **exception_fields(error))
    text = journal.path.read_text()
    assert "private-api-key" not in text
    event = json.loads(text)
    assert event["frames"][-1]["function"] == "test_exception_frames_keep_locations_without_messages_or_locals"
    assert event["frames"][-1]["line"] > 0
