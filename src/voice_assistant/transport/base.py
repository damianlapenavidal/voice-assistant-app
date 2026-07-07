"""Abstract transport interface for device communication."""

from __future__ import annotations

from abc import ABC, abstractmethod

from voice_assistant.core.message import Message


class TransportError(Exception):
    """Raised when a transport operation fails."""


class Transport(ABC):
    """Abstract base class for device transports (WebSocket, Bluetooth, etc.)."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send_message(self, message: Message) -> None: ...

    @abstractmethod
    async def receive_message(self) -> Message: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...
