"""Text utilities: message splitting, truncation, etc."""

from __future__ import annotations


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split a long text into chunks no longer than max_length characters."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Try to split on a newline near max_length
        split_pos = text.rfind("\n", 0, max_length)
        if split_pos == -1:
            split_pos = max_length
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")
    return chunks


def truncate(text: str, max_chars: int = 200, ellipsis: str = "…") -> str:
    """Return text truncated to max_chars with an optional ellipsis."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(ellipsis)] + ellipsis
