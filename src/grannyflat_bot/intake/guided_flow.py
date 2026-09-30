"""引导式连续提问：报价必填项引导；产品/FAQ 问题优先用知识库回答。

默认三问（与 REQUIRED_FOR_QUOTE 一致）：
1. 州 state
2. 卧室 bedrooms
3. 交付方式 delivery_type

文案与顺序可在「教模型」编辑 data/prompts/guided_intake.yaml。
客户若在问产品/流程等开放问题，不应被引导问句整段覆盖。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from grannyflat_bot.config import DATA_DIR
from grannyflat_bot.intake.schema import REQUIRED_FOR_QUOTE, RequirementState
from grannyflat_bot.llm.prompts import ReplyLocale

logger = logging.getLogger(__name__)

GUIDED_CONFIG_PATH = DATA_DIR / "prompts" / "guided_intake.yaml"

# RequirementState 上允许出现在引导流程里的字段
_ALLOWED_FIELDS = set(RequirementState.model_fields) - {
    "field_sources",
    "field_confidence",
}


@dataclass(frozen=True)
class GuidedStep:
    field: str
    question_en: str
    question_zh: str
    hint_en: str = ""
    hint_zh: str = ""


DEFAULT_GUIDED_STEPS: list[GuidedStep] = [
    GuidedStep(
        field="state",
        question_en="Could you share the site suburb and postcode, or the street address?",
        question_zh="方便提供项目所在城区和邮编，或街道地址吗？",
        hint_en="This helps identify the state; council may need a more precise location.",
        hint_zh="用于确认州；确定 council 可能需要更具体的位置。",
    ),
    GuidedStep(
        field="bedrooms",
        question_en="How many bedrooms? (studio / 1 / 2 / 3)",
        question_zh="需要几间卧室？（studio / 1 / 2 / 3）",
        hint_en="Studio counts as 0 bedrooms",
        hint_zh="studio 按 0 间卧室计算",
    ),
    GuidedStep(
        field="delivery_type",
        question_en="Which delivery type do you prefer: kit, managed_build, or turnkey?",
        question_zh="更倾向哪种交付方式：kit（模块供应）、managed_build（协调建造）、turnkey（交钥匙）？",
        hint_en="kit = supply focus; turnkey = broader handover path",
        hint_zh="kit=偏供货；turnkey=更接近交房路径",
    ),
]

_cached_steps: list[GuidedStep] | None = None
_cached_mtime: float | None = None


def guided_config_path() -> Path:
    return GUIDED_CONFIG_PATH


def get_guided_steps() -> list[GuidedStep]:
    """当前生效的引导步骤（优先读 YAML，失败则用默认）。"""
    global _cached_steps, _cached_mtime
    path = GUIDED_CONFIG_PATH
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except OSError:
        mtime = None
    if _cached_steps is not None and mtime == _cached_mtime:
        return _cached_steps
    steps = _load_steps_from_disk(path)
    _cached_steps = steps
    _cached_mtime = mtime
    return steps


def reload_guided_steps() -> dict[str, Any]:
    """教模型保存后强制重载。"""
    global _cached_steps, _cached_mtime
    _cached_steps = None
    _cached_mtime = None
    steps = get_guided_steps()
    return {
        "ok": True,
        "path": str(GUIDED_CONFIG_PATH),
        "steps": [s.field for s in steps],
        "count": len(steps),
    }


def _load_steps_from_disk(path: Path) -> list[GuidedStep]:
    if not path.exists():
        return list(DEFAULT_GUIDED_STEPS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("guided_intake.yaml parse failed: %s — using defaults", exc)
        return list(DEFAULT_GUIDED_STEPS)

    rows = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        logger.warning("guided_intake.yaml has no steps — using defaults")
        return list(DEFAULT_GUIDED_STEPS)

    out: list[GuidedStep] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field") or "").strip()
        if not field or field in seen or field not in _ALLOWED_FIELDS:
            if field and field not in _ALLOWED_FIELDS:
                logger.warning("guided step skipped unknown field: %s", field)
            continue
        q_en = str(row.get("question_en") or "").strip()
        q_zh = str(row.get("question_zh") or q_en).strip()
        if not q_en:
            continue
        seen.add(field)
        out.append(
            GuidedStep(
                field=field,
                question_en=q_en,
                question_zh=q_zh or q_en,
                hint_en=str(row.get("hint_en") or "").strip(),
                hint_zh=str(row.get("hint_zh") or "").strip(),
            )
        )
    if not out:
        return list(DEFAULT_GUIDED_STEPS)
    return out


def next_missing_step(requirements: RequirementState) -> GuidedStep | None:
    data = requirements.model_dump()
    for step in get_guided_steps():
        if data.get(step.field) is None:
            return step
    return None


def guided_complete(requirements: RequirementState) -> bool:
    return next_missing_step(requirements) is None


def format_guided_question(
    step: GuidedStep,
    *,
    locale: ReplyLocale = "en",
    progress_index: int | None = None,
) -> str:
    steps = get_guided_steps()
    total = len(steps)
    idx = progress_index
    if idx is None:
        idx = next(i for i, s in enumerate(steps) if s.field == step.field) + 1

    if locale == "bilingual":
        return (
            f"为了解项目需求，请回答第 {idx}/{total} 个问题：\n"
            f"{step.question_zh}\n"
            f"（{step.hint_zh}）\n\n"
            f"---\n\n"
            f"To understand your project, question {idx}/{total}:\n"
            f"{step.question_en}\n"
            f"({step.hint_en})"
        )
    return (
        f"To understand your project, question {idx}/{total}:\n"
        f"{step.question_en}\n"
        f"({step.hint_en})"
    )


def format_intake_complete(*, locale: ReplyLocale = "en") -> str:
    en = "I'll review the details available and confirm the next steps for your site."
    if locale == "bilingual":
        return "我会核对已有信息，再为您的项目确认下一步。\n\n---\n\n" + en
    return en


def user_wants_skip_guided(text: str) -> bool:
    lower = (text or "").lower().strip()
    triggers = (
        "skip",
        "跳过",
        "先问别的",
        "随便问问",
        "先看看",
        "faq only",
        "just asking",
    )
    return any(t in lower for t in triggers)


def looks_like_open_question(text: str) -> bool:
    """客户在问产品/流程等，而不是单纯填引导答案。"""
    s = (text or "").strip()
    if not s:
        return False
    lower = s.lower()
    if "?" in s or "？" in s:
        return True
    if re.search(r"(吗|呢|么|如何|怎么|怎样|是否|有没有|能不能|可以吗)", s):
        return True
    q_en = (
        "how ",
        "what ",
        "where ",
        "when ",
        "why ",
        "which ",
        "do you",
        "does ",
        "can you",
        "is there",
        "are there",
        "have you",
        "any ",
    )
    if any(k in lower for k in q_en):
        return True
    topics = (
        "施工",
        "团队",
        "builder",
        "install",
        "warranty",
        "质保",
        "ship",
        "海运",
        "运输",
        "permit",
        "审批",
        "council",
        "asbestos",
        "石棉",
        "team",
        "local",
        "本地",
    )
    return any(k in lower or k in s for k in topics)


def looks_like_guided_field_answer(text: str, step: GuidedStep | None) -> bool:
    """短答或明显在回答当前引导字段。"""
    s = (text or "").strip()
    if not s or step is None:
        return False
    lower = s.lower()
    if len(s) > 80 and looks_like_open_question(s):
        return False
    if step.field == "state":
        states = (
            "nsw",
            "vic",
            "qld",
            "sa",
            "wa",
            "tas",
            "act",
            "nt",
            "sydney",
            "melbourne",
            "brisbane",
            "perth",
            "adelaide",
            "悉尼",
            "墨尔本",
            "布里斯班",
        )
        return any(k in lower or k in s for k in states) and not looks_like_open_question(
            s
        )
    if step.field == "bedrooms":
        if looks_like_open_question(s):
            return False
        return bool(
            re.search(r"\b([0-3]|studio|bedsitter|bedrooms?|bed)\b", lower)
            or re.search(r"[0-3]\s*房|卧室|一室|两室|三室|studio", s)
        )
    if step.field == "delivery_type":
        if looks_like_open_question(s):
            return False
        return any(
            k in lower for k in ("kit", "managed_build", "managed", "turnkey", "全包", "交钥匙")
        )
    # 其它字段：短答视为在填当前题
    if looks_like_open_question(s):
        return False
    return len(s) <= 80


def prefer_knowledge_over_guided(
    text: str,
    step: GuidedStep | None,
    *,
    wants_quote: bool,
) -> bool:
    """引导未齐时：开放问题/产品问答优先保留知识库回复。"""
    if step is None or wants_quote:
        return False
    if looks_like_guided_field_answer(text, step):
        return False
    return looks_like_open_question(text)


def guided_progress(requirements: RequirementState) -> dict[str, Any]:
    steps = get_guided_steps()
    missing = requirements.missing_fields([s.field for s in steps])
    step = next_missing_step(requirements)
    return {
        "complete": not missing,
        "missing": missing,
        "next_field": step.field if step else None,
        "steps": [s.field for s in steps],
        "quote_required": list(REQUIRED_FOR_QUOTE),
    }
