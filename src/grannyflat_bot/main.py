from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from grannyflat_bot.channels.cli import CliChannel
from grannyflat_bot.channels.base import OutboundMessage
from grannyflat_bot.config import get_settings
from grannyflat_bot.orchestrator import ChatOrchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Granny flat sales chatbot (CLI / web)"
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Run FastAPI web chat UI instead of CLI",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web port (default 8000)",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Force offline mock LLM (no API calls)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional fixed CLI session id",
    )
    return parser


async def run_cli(session_id: str | None = None) -> int:
    settings = get_settings()
    channel = CliChannel(session_id=session_id)
    orchestrator = ChatOrchestrator()

    mode = "LIVE LLM" if settings.use_live_llm else "MOCK LLM"
    await channel.send_text(
        OutboundMessage(
            session_id=channel.session_id,
            text=(
                f"Granny Flat sales assistant ({mode})\n"
                f"Session: {channel.session_id}\n"
                "Ask about products, council basics, or request a quote.\n"
                "Type quit to exit."
            ),
        )
    )

    while True:
        inbound = await channel.receive()
        if inbound is None:
            print("Bye.")
            return 0
        result = orchestrator.handle(inbound.session_id, inbound.text)
        await channel.send_text(
            OutboundMessage(session_id=inbound.session_id, text=result.reply)
        )


def run_web(host: str, port: int) -> int:
    import sys
    from pathlib import Path

    import uvicorn

    # Ensure src layout works even when launched without PYTHONPATH
    src = Path(__file__).resolve().parents[1]
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    uvicorn.run(
        "grannyflat_bot.app:app",
        host=host,
        port=port,
        reload=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.mock_llm:
        os.environ["MOCK_LLM"] = "1"
        get_settings.cache_clear()

    if args.web:
        return run_web(args.host, args.port)
    return asyncio.run(run_cli(session_id=args.session_id))


if __name__ == "__main__":
    sys.exit(main())
