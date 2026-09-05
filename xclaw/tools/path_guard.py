"""Security: Path guard – blocks access to sensitive filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path

# Paths that must never be accessible via file tools
BLOCKED_PATH_FRAGMENTS = [
    ".ssh",
    ".aws",
    ".env",
    ".git/config",
    "credentials",
    "secrets",
    "private_key",
    ".gnupg",
    ".config/gcloud",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    "shadow",
    "passwd",
]


def is_path_safe(path: str) -> bool:
    """Return True if the path is considered safe (no blocked fragments).

    Normalises the path and performs a case-insensitive substring check
    against every blocked fragment.
    """
    normalised = os.path.normpath(path).replace("\\", "/").lower()
    return not any(fragment in normalised for fragment in BLOCKED_PATH_FRAGMENTS)


def assert_path_safe(path: str) -> None:
    """Raise PermissionError if the path contains a blocked fragment."""
    if not is_path_safe(path):
        raise PermissionError(f"Access to path is not allowed: {path!r}")


def resolve_tool_path(path: str, workspace_root: Path | None = None) -> Path:
    assert_path_safe(path)
    resolved = Path(path).expanduser()
    if workspace_root is not None:
        root = workspace_root.resolve()
        resolved = (resolved if resolved.is_absolute() else root / resolved).resolve()
        if not resolved.is_relative_to(root):
            raise PermissionError("File access is outside the current chat workspace")
    return resolved
