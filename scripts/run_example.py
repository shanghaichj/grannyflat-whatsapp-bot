"""Run a short end-to-end conversation example (mock-friendly)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("MOCK_LLM", "1")

from grannyflat_bot.config import get_settings
from grannyflat_bot.orchestrator import ChatOrchestrator
from grannyflat_bot.session.store import SessionStore


def _safe_print(text: str) -> None:
    """Avoid Windows GBK console crashes on bullets / dashes."""
    cleaned = (
        text.replace("\u2022", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    try:
        print(cleaned)
    except UnicodeEncodeError:
        print(cleaned.encode("ascii", "replace").decode("ascii"))


def main() -> int:
    get_settings.cache_clear()
    store = SessionStore(db_path=ROOT / "data" / "sessions" / "example.sqlite3")
    orch = ChatOrchestrator(sessions=store)
    session_id = "example-demo"

    turns = [
        "Hi, we are looking at a granny flat in VIC.",
        "We want a 2 bedroom turnkey, about 60m2, with foundations and services included.",
        "Please give me a quote.",
    ]

    _safe_print("=" * 60)
    _safe_print("Granny Flat Bot - example conversation")
    _safe_print(f"Mode: {'LIVE' if get_settings().use_live_llm else 'MOCK'}")
    _safe_print("=" * 60)

    for text in turns:
        _safe_print(f"\nYOU: {text}")
        result = orch.handle(session_id, text)
        _safe_print(f"\nBOT:\n{result.reply}")
        _safe_print("\n--- questionnaire ---")
        _safe_print(str(result.questionnaire))
        if result.quote and result.quote.can_quote:
            _safe_print(
                f"--- quote mid ${result.quote.subtotal:,.0f} "
                f"range ${result.quote.range_low:,.0f}-{result.quote.range_high:,.0f} ---"
            )

    _safe_print("\nDone. Open http://127.0.0.1:8000/ for the visual demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
