"""客服中英互译：客服中文→英文对客；客户英文→中文给客服看。"""

from __future__ import annotations

import re
from typing import Literal

from grannyflat_bot.llm.client import LLMClient

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# Latin letters (enough to treat as English-ish for AU customers)
_LATIN_RE = re.compile(r"[A-Za-z]")


def contains_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def looks_primarily_english(text: str) -> bool:
    """True when text has Latin letters and little/no CJK (customer English)."""
    s = (text or "").strip()
    if not s:
        return False
    if contains_chinese(s):
        # bilingual blocks often have both — still treat as needing no EN→ZH if already CN-heavy
        cjk = len(_CJK_RE.findall(s))
        latin = len(_LATIN_RE.findall(s))
        return latin > 0 and cjk * 3 < latin
    return bool(_LATIN_RE.search(s))


def translate(
    text: str,
    *,
    target: Literal["en", "zh"],
    llm: LLMClient | None = None,
) -> str:
    """Translate text. Returns original on empty/failure/mock when unchanged needed."""
    body = (text or "").strip()
    if not body:
        return body

    if target == "en" and not contains_chinese(body):
        return body
    if target == "zh" and contains_chinese(body) and not looks_primarily_english(body):
        return body

    client = llm or LLMClient()
    if target == "en":
        system = (
            "You are a professional translator for Australian granny flat sales. "
            "Translate the user message from Chinese to natural Australian English. "
            "Output ONLY the English translation, no quotes or commentary."
        )
    else:
        system = (
            "你是澳洲 Granny Flat 销售客服的翻译助手。"
            "把用户消息从英文译成简洁自然的简体中文，供国内客服阅读。"
            "只输出中文译文，不要引号或解释。"
        )

    try:
        out = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": body},
            ],
            temperature=0.1,
        ).strip()
        return out or body
    except Exception:
        return body


def zh_to_en(text: str, *, llm: LLMClient | None = None) -> str:
    return translate(text, target="en", llm=llm)


def en_to_zh(text: str, *, llm: LLMClient | None = None) -> str:
    return translate(text, target="zh", llm=llm)


def english_only_for_customer(
    text: str, *, llm: LLMClient | None = None
) -> str:
    """对客文案：去掉中文 / 双语块，只保留英文（WhatsApp 澳洲客户）。"""
    body = (text or "").strip()
    if not body:
        return body

    if "\n---\n" in body:
        parts = [p.strip() for p in body.split("\n---\n") if p.strip()]
        no_cjk = [p for p in parts if not contains_chinese(p)]
        if no_cjk:
            # 双语块通常后半段是英文；多段无中文时取最长
            body = max(no_cjk, key=len)
        elif parts:
            body = max(parts, key=lambda p: len(_LATIN_RE.findall(p)))

    if not contains_chinese(body):
        return body

    translated = zh_to_en(body, llm=llm).strip()
    if translated and not contains_chinese(translated):
        return translated

    # 翻译仍夹杂中文时：去掉含汉字的行
    lines = [
        ln for ln in (translated or body).splitlines() if not contains_chinese(ln)
    ]
    cleaned = "\n".join(lines).strip()
    return cleaned or "I'll check the details and get back to you."
