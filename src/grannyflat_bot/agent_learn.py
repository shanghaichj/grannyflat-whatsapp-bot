"""从客服人工回复自动收录话术（供知识检索复用）。"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grannyflat_bot.config import get_settings
from grannyflat_bot.teach_store import PROMPTS_DIR, ensure_prompt_defaults

logger = logging.getLogger(__name__)

LEARNED_FILE = "learned_from_agents.md"
_MIN_QUESTION = 8
_MIN_ANSWER = 24


def learned_scripts_path() -> Path:
    ensure_prompt_defaults()
    return PROMPTS_DIR / LEARNED_FILE


def last_user_message(history: list[dict[str, Any]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            return str(item.get("content", "")).strip()
    return ""


def record_agent_exchange(
    *,
    session_id: str,
    channel: str,
    history: list[dict[str, Any]],
    agent_reply: str,
    agent_name: str = "客服",
    llm: Any | None = None,
) -> dict[str, Any]:
    """客服发消息后：取上一轮客户问题 + 本条回复，写入 learned_from_agents.md。"""
    settings = get_settings()
    if not settings.agent_learn_enabled:
        return {"saved": False, "reason": "disabled"}

    question = last_user_message(history)
    answer = (agent_reply or "").strip()
    if len(question) < _MIN_QUESTION or len(answer) < settings.agent_learn_min_chars:
        return {"saved": False, "reason": "too_short"}

    entry = _build_entry(
        question=question,
        answer=answer,
        session_id=session_id,
        channel=channel,
        agent_name=agent_name,
        llm=llm,
        use_llm=settings.agent_learn_use_llm and llm is not None and llm.live,
    )

    path = learned_scripts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_file_header(), encoding="utf-8")

    existing = path.read_text(encoding="utf-8")
    key = _dedupe_key(question)
    if f"key:{key}" in existing:
        return {"saved": False, "reason": "duplicate"}

    with path.open("a", encoding="utf-8") as f:
        f.write(entry)

    return {
        "saved": True,
        "file": LEARNED_FILE,
        "question_preview": question[:120],
    }


def record_training_script(
    *,
    question: str,
    answer: str,
    llm: Any | None = None,
) -> dict[str, Any]:
    """话术训练页：手动收录模拟客户问题 + 标准答法。"""
    history = [{"role": "user", "content": question.strip()}]
    return record_agent_exchange(
        session_id="teach-practice",
        channel="teach_practice",
        history=history,
        agent_reply=answer.strip(),
        agent_name="话术训练",
        llm=llm,
    )


def load_learned_scripts_text() -> str:
    path = learned_scripts_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _file_header() -> str:
    return (
        "# 客服人工回复自动收录 / Learned from agent replies\n\n"
        "以下条目由客服工作台人工回复自动生成，可在「教模型」页编辑或删除。\n"
        "相似客户问题会优先检索这些片段。\n\n"
    )


def _dedupe_key(question: str) -> str:
    norm = re.sub(r"\s+", " ", question.lower().strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _build_entry(
    *,
    question: str,
    answer: str,
    session_id: str,
    channel: str,
    agent_name: str,
    llm: Any | None,
    use_llm: bool,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    key = _dedupe_key(question)
    body = ""
    if use_llm and llm is not None:
        try:
            body = _summarize_with_llm(llm, question=question, answer=answer, channel=channel)
        except Exception:
            logger.exception("Agent learn LLM summarize failed")
            body = ""

    if not body:
        body = (
            f"**客户常问：** {question.strip()}\n\n"
            f"**推荐答法（来自 {agent_name}）：** {answer.strip()}\n"
        )

    return (
        f"\n<!-- key:{key} session:{session_id} -->\n"
        f"### {ts} · {channel}\n"
        f"{body.strip()}\n"
    )


def _summarize_with_llm(
    llm: Any,
    *,
    question: str,
    answer: str,
    channel: str,
) -> str:
    """把单次 Q&A 整理成可复用话术条目（去掉客户隐私）。"""
    lang = "English"
    prompt = f"""Turn this customer support exchange into a reusable talk-script entry for future similar questions.

Rules:
- Output markdown bullet points only (no JSON).
- Remove names, phone numbers, exact addresses, and specific AUD prices.
- Keep the sales consultant tone and facts from the agent reply.
- Reply language style for customers: {lang}
- Never tell future replies to say bot/AI/handoff/转人工/资料无法回答.
- Include: what the customer typically asks, recommended reply approach, and what to avoid.

Customer question:
{question}

Agent reply:
{answer}
"""
    text = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "You write concise reusable sales talk scripts. "
                    "Customer-facing wording must sound like a human consultant."
                ),
            },
            {"role": "user", "content": prompt},
        ]
    )
    return str(text or "").strip()
