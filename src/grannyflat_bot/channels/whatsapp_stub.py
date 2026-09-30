from __future__ import annotations

from typing import Any

from grannyflat_bot.channels.base import Channel, InboundMessage, OutboundMessage


class WhatsAppChannelStub(Channel):
    """Placeholder for Meta Cloud API or Twilio WhatsApp.

    MVP does not send real WhatsApp messages. Wire webhook handlers here later:
    - Meta: verify token GET + signed POST webhooks
    - Twilio: form-encoded inbound webhook + REST send
    """

    name = "whatsapp_stub"

    def __init__(self) -> None:
        self.outbox: list[OutboundMessage] = []
        self.inbox: list[InboundMessage] = []

    async def send_text(self, message: OutboundMessage) -> None:
        self.outbox.append(message)

    async def receive(self) -> InboundMessage | None:
        if not self.inbox:
            return None
        return self.inbox.pop(0)

    def enqueue_inbound(
        self,
        text: str,
        *,
        session_id: str,
        sender_id: str = "",
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.inbox.append(
            InboundMessage(
                session_id=session_id,
                text=text,
                sender_id=sender_id,
                channel=self.name,
                raw=raw or {},
            )
        )

    def parse_meta_webhook(self, payload: dict[str, Any]) -> list[InboundMessage]:
        """Parse a Meta Cloud API webhook payload into inbound messages.

        Not connected to a live endpoint in MVP — use for future integration tests.
        """
        messages: list[InboundMessage] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue
                    wa_id = msg.get("from", "")
                    messages.append(
                        InboundMessage(
                            session_id=f"wa-{wa_id}",
                            text=msg.get("text", {}).get("body", ""),
                            sender_id=wa_id,
                            channel="whatsapp_meta",
                            raw=msg,
                        )
                    )
        return messages

    def parse_twilio_form(self, form: dict[str, str]) -> InboundMessage:
        """Parse a Twilio WhatsApp inbound form body."""
        from_number = form.get("From", "").removeprefix("whatsapp:")
        return InboundMessage(
            session_id=f"wa-{from_number}",
            text=form.get("Body", ""),
            sender_id=from_number,
            channel="whatsapp_twilio",
            raw=dict(form),
        )
