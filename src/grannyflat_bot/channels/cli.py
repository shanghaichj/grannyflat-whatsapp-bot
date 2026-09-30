from __future__ import annotations

import uuid

from grannyflat_bot.channels.base import Channel, InboundMessage, OutboundMessage


class CliChannel(Channel):
    """Interactive stdin/stdout channel for local demos."""

    name = "cli"

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or f"cli-{uuid.uuid4().hex[:8]}"

    async def send_text(self, message: OutboundMessage) -> None:
        print()
        print(message.text)
        print()

    async def receive(self) -> InboundMessage | None:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not text:
            return InboundMessage(
                session_id=self.session_id,
                text="",
                sender_id="cli-user",
                channel=self.name,
            )
        if text.lower() in {"quit", "exit", "/quit", "/exit"}:
            return None
        return InboundMessage(
            session_id=self.session_id,
            text=text,
            sender_id="cli-user",
            channel=self.name,
        )
