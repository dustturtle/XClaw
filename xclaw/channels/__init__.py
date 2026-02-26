"""Channel adapter base class."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ChannelAdapter(ABC):
    """Abstract base class for all channel adapters."""

    @abstractmethod
    async def start(self) -> None:
        """Start listening for incoming messages."""
        ...

    @abstractmethod
    async def send_response(self, chat_id: str, text: str) -> None:
        """Send a text response to a chat."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the adapter gracefully."""
        ...
