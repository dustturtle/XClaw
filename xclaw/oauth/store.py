"""Persistent storage for provider auth profiles."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - Windows fallback is exercised implicitly
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class AuthProfileStore:
    """JSON-backed auth profile storage with best-effort file locking."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        return self.load().get("profiles", {}).get(profile_id)

    def set_profile(self, profile_id: str, profile: dict[str, Any]) -> None:
        with self._locked():
            data = self._read_unlocked()
            profiles = data.setdefault("profiles", {})
            profiles[profile_id] = profile
            self._write_unlocked(data)

    def delete_profile(self, profile_id: str) -> None:
        with self._locked():
            data = self._read_unlocked()
            profiles = data.setdefault("profiles", {})
            profiles.pop(profile_id, None)
            self._write_unlocked(data)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with lock_path.open("a+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"profiles": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"profiles": {}}

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
