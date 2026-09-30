"""CRM-side sales intelligence, kept separate from customer reply generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.session.contacts import (
    PORT_CITY_IDS,
    SALES_STAGE_NEXT_ACTIONS,
    match_major_city,
)


QUALIFICATION_LABELS = {
    "early": "资料较少",
    "developing": "持续确认",
    "sales_ready": "可销售跟进",
}

LEAD_TEMPERATURE_LABELS = {
    "cold": "咨询客户",
    "warm": "意向客户",
    "hot": "重点客户",
    "non_target": "非目标客户",
}


@dataclass(frozen=True)
class QualificationResult:
    score: int
    level: str
    missing: list[str]
    temperature: str
    reasons: list[str]


def qualify_lead(
    requirements: RequirementState,
    *,
    history: list[dict[str, Any]],
    sales_stage: str,
    whatsapp_phone: str | None = None,
    contact_type: str | None = None,
    sent_template_keys: list[str] | None = None,
) -> QualificationResult:
    """Deterministic, explainable qualification score; never asks an LLM to invent facts."""
    score = 0
    missing: list[str] = []

    checks: list[tuple[bool, int, str]] = [
        (requirements.state is not None, 10, "州"),
        (
            bool(
                requirements.street_address
                or requirements.suburb_or_lga
                or requirements.postcode
            ),
            15,
            "项目地址/城区",
        ),
        (requirements.bedrooms is not None, 15, "卧室数量"),
        (requirements.lot_size_m2 is not None, 10, "土地面积"),
        (
            requirements.budget_aud_min is not None
            or requirements.budget_aud_max is not None,
            15,
            "预算",
        ),
        (bool(requirements.timeline), 10, "计划时间"),
        (requirements.delivery_type is not None, 10, "交付方式"),
        (bool(requirements.usage_type), 5, "项目用途"),
        (bool(requirements.contact_name), 5, "客户姓名"),
        (
            bool(
                whatsapp_phone
                or requirements.contact_phone
                or requirements.contact_email
            ),
            5,
            "联系方式",
        ),
    ]
    for present, points, label in checks:
        if present:
            score += points
        else:
            missing.append(label)

    if sum(1 for item in history if item.get("role") == "user") >= 2:
        score = min(100, score + 5)

    reasons: list[str] = []
    role = (contact_type or requirements.contact_type or "").strip().lower()
    usage = (requirements.usage_type or "").strip().lower()
    is_builder = role == "builder" or usage == "builder"
    is_investor = role == "investor" or usage == "investment"
    is_multi_unit = bool(requirements.unit_count and requirements.unit_count >= 2)

    major_city = match_major_city(
        suburb=requirements.suburb_or_lga,
        postcode=requirements.postcode,
        text=" ".join(
            value
            for value in (requirements.suburb_or_lga, requirements.street_address)
            if value
        ),
    )
    if major_city and major_city["id"] in PORT_CITY_IDS:
        score = min(100, score + 10)
        reasons.append(f"靠近港口大城市：{major_city['label_zh']}（+10）")

    # A confirmed postcode outside the configured service metros is non-target.
    # Missing/uncertain location never triggers this rule.
    non_target = bool(requirements.postcode and not major_city)
    if non_target:
        reasons.append("已确认地址不在当前目标城市服务范围")

    if is_builder and is_investor:
        score = max(score, 95)
        reasons.append("投资项目且客户为建造商（至少95分）")
    elif is_builder:
        score = max(score, 90)
        reasons.append("客户为建造商（至少90分）")
    elif is_investor:
        score = max(score, 80)
        reasons.append("客户为投资客（至少80分）")
    if is_multi_unit:
        score = max(score, 90)
        reasons.append(f"计划购买{requirements.unit_count}套（至少90分）")

    quote_follow_up, quote_accepted = _quote_intent(history, sent_template_keys or [])
    if quote_accepted:
        reasons.append("客户在收到报价后明确表示可以接受或希望继续")
    elif quote_follow_up:
        reasons.append("客户在收到报价后继续询问项目细节")

    if score >= 70:
        level = "sales_ready"
    elif score >= 40:
        level = "developing"
    else:
        level = "early"

    if non_target:
        temperature = "non_target"
    elif is_builder or is_investor or is_multi_unit or score >= 80:
        temperature = "hot"
    elif quote_accepted or quote_follow_up or (
        sales_stage in {"qualified", "design_discussion", "quotation", "site_assessment", "contract"}
        and score >= 50
    ):
        temperature = "warm"
    else:
        temperature = "cold"
    if not reasons:
        reasons.append("当前以一般咨询和已收集资料为依据")
    return QualificationResult(score, level, missing, temperature, reasons)


def _quote_intent(
    history: list[dict[str, Any]], sent_template_keys: list[str]
) -> tuple[bool, bool]:
    """Return (asked_details_after_quote, explicitly_accepted_quote)."""
    quote_index = -1
    for index, item in enumerate(history):
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        template = str(extra.get("template_name") or "").lower()
        content = str(item.get("content") or "").lower()
        if template.endswith("price") or (
            "whatsapp template sent:" in content and "price" in content
        ):
            quote_index = index
    if quote_index < 0 and not any(str(key).lower().endswith("price") for key in sent_template_keys):
        return False, False
    messages = [
        str(item.get("content") or "").strip().lower()
        for item in history[quote_index + 1 :]
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ] if quote_index >= 0 else []
    if not messages:
        return False, False
    combined = " ".join(messages)
    accepted = any(
        phrase in combined
        for phrase in (
            "acceptable", "sounds good", "works for me", "go ahead", "proceed",
            "i accept", "interested", "可以接受", "接受报价", "继续推进", "有兴趣",
        )
    )
    detail_terms = (
        "detail", "include", "delivery", "install", "builder", "council",
        "approval", "timeline", "payment", "bedroom", "bathroom", "design",
        "floor plan", "warranty", "细节", "包括", "运输", "安装", "审批",
        "工期", "付款", "户型", "保修",
    )
    follow_up = accepted or "?" in combined or any(term in combined for term in detail_terms)
    return follow_up, accepted


def deterministic_summary(
    requirements: RequirementState,
    *,
    history: list[dict[str, Any]],
    sales_stage: str,
) -> str:
    facts: list[str] = []
    if requirements.contact_name:
        facts.append(f"客户：{requirements.contact_name}")
    location = " ".join(
        str(value)
        for value in (
            requirements.street_address,
            requirements.suburb_or_lga,
            _enum_value(requirements.state),
            requirements.postcode,
        )
        if value
    )
    if location:
        facts.append(f"项目位置：{location}")
    if requirements.bedrooms is not None:
        facts.append(f"需求：{requirements.bedrooms}卧")
    if requirements.approx_floor_area_m2 is not None:
        facts.append(f"约{requirements.approx_floor_area_m2:g}㎡")
    if requirements.delivery_type is not None:
        facts.append(f"交付：{_enum_value(requirements.delivery_type)}")
    if requirements.budget_aud_min is not None or requirements.budget_aud_max is not None:
        low = requirements.budget_aud_min
        high = requirements.budget_aud_max
        budget = f"{low:g}–{high:g}" if low is not None and high is not None else f"{(low if low is not None else high):g}"
        facts.append(f"预算：AUD {budget}")
    if requirements.timeline:
        facts.append(f"时间：{requirements.timeline}")

    last_customer = next(
        (
            str(item.get("content") or "").strip()
            for item in reversed(history)
            if item.get("role") == "user" and item.get("content")
        ),
        "",
    )
    if last_customer:
        compact = " ".join(last_customer.split())[:180]
        facts.append(f"最近关注：{compact}")
    if not facts:
        return "新线索，尚未收集到有效项目资料。"
    return "；".join(facts) + f"。当前阶段：{sales_stage}。"


def safe_internal_text(value: Any, *, max_length: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:max_length] or None


def recommended_follow_up_hours(sales_stage: str) -> int | None:
    return {
        "new_lead": 24,
        "contacted": 24,
        "qualified": 48,
        "design_discussion": 48,
        "quotation": 24,
        "site_assessment": 48,
        "contract": 72,
        "closed": None,
    }.get(sales_stage, 24)


def follow_up_datetime(hours: Any, *, sales_stage: str) -> str | None:
    if sales_stage == "closed":
        return None
    try:
        value = int(hours) if hours is not None else recommended_follow_up_hours(sales_stage)
    except (TypeError, ValueError):
        value = recommended_follow_up_hours(sales_stage)
    if value is None:
        return None
    value = max(1, min(value, 24 * 30))
    return (datetime.now(timezone.utc) + timedelta(hours=value)).isoformat()


def default_next_action(sales_stage: str) -> str | None:
    return SALES_STAGE_NEXT_ACTIONS.get(sales_stage)


def generate_ai_summary(llm: Any, *, requirements: RequirementState, history: list[dict[str, Any]], sales_stage: str) -> tuple[str, str]:
    """生成中文内部摘要，返回 (文本, 来源)。

    来源为 "llm"（模型真实生成成功）或 "rules"（确定性兜底，含 LLM 不可用/报错）。
    调用方必须以此来源为准，不能只看 llm.live 配置项——配置存在不代表调用成功。
    """
    fallback = deterministic_summary(
        requirements, history=history, sales_stage=sales_stage
    )
    transcript = "\n".join(
        f"{item.get('role')}: {str(item.get('content') or '')[:500]}"
        for item in history[-12:]
        if item.get("role") in ("user", "assistant", "agent")
    )
    try:
        data = llm.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You write a concise internal CRM summary in Simplified Chinese. "
                        "Use only supplied facts. Do not invent price, approval, council, "
                        "budget, timeline or customer intent. Return JSON with summary only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Sales stage: {sales_stage}\n"
                        f"Requirements: {requirements.model_dump_json(exclude_none=True)}\n"
                        f"Recent conversation:\n{transcript}"
                    ),
                },
            ]
        )
        text = safe_internal_text(data.get("summary"), max_length=1000)
        if text:
            return text, "llm"
    except Exception:
        pass
    return fallback, "rules"


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))
