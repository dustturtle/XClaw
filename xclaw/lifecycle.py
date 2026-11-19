"""Small, private structured lifecycle journal, independent of model logging."""

from datetime import datetime, timezone
import json
import os
import sys
import traceback
from pathlib import Path
import uuid


def exception_fields(error: BaseException) -> dict:
    return {
        "exception_type": type(error).__name__,
        "frames": [{"file": f.filename, "line": f.lineno, "function": f.name}
                   for f in traceback.extract_tb(error.__traceback__)[-16:]],
    }


class LifecycleJournal:
    def __init__(self, directory: Path, role: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "lifecycle.jsonl"
        self.role = role
        self.boot_id = uuid.uuid4().hex

    def emit(self, event: str, **fields) -> None:
        allowed = {"child_pid", "component", "exit_code", "signal", "exception_type",
                   "healthy", "duration_seconds", "receipt_id", "run_id", "frames"}
        if event == "request_stage_timing":
            allowed.update({"elapsed_ms", "outcome", "stages", "dropped"})
        row = {
            "time": datetime.now(timezone.utc).isoformat(), "event": event,
            "role": self.role, "boot_id": self.boot_id, "pid": os.getpid(),
            "ppid": os.getppid(),
            **{k: v for k, v in fields.items() if k in allowed},
        }
        data = (json.dumps(row, ensure_ascii=True) + "\n").encode()
        try:
            fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            sys.stderr.write("Lifecycle journal unavailable; event not persisted.\n")
