from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """Normalized inbound message from any channel."""

    session_id: str
    text: str
    sender_id: str = ""
    channel: str = "unknown"
    profile_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """Normalized outbound message to any channel."""

    session_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Channel(ABC):
    """Transport adapter. Core chat logic never depends on WhatsApp specifics."""

    name: str = "base"

    @abstractmethod
    async def send_text(self, message: OutboundMessage) -> None:
        """Deliver a text reply to the customer."""

    @abstractmethod
    async def receive(self) -> InboundMessage | None:
        """Pull or wait for the next inbound message (CLI / pollers)."""
