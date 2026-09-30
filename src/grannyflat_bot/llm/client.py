from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from grannyflat_bot.config import Settings, get_settings


def _ascii_header(value: str, fallback: str) -> str:
    """HTTP 头只能是 ASCII；中文标题等会导致 UnicodeEncodeError。"""
    text = (value or "").strip()
    if not text:
        return fallback
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return fallback


def _clean_api_key(value: str) -> str:
    """去掉首尾空白/中英文引号/零宽字符；密钥必须是纯 ASCII。"""
    import re

    text = (value or "").strip()
    for ch in (
        '"',
        "'",
        "\u201c",
        "\u201d",
        "\u2018",
        "\u2019",
        "\u300c",
        "\u300d",
        "\uff02",
        "\u3000",
        "\ufeff",
        "\u200b",
        "\xa0",
    ):
        text = text.strip(ch)
    text = text.strip()

    # 全角英数字 → 半角（记事本/中文输入法常见）
    normalized: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            normalized.append(chr(code - 0xFEE0))
        elif ch == "\u3000":
            normalized.append(" ")
        else:
            normalized.append(ch)
    text = "".join(normalized)

    ascii_only = "".join(ch for ch in text if ord(ch) < 128)
    match = re.search(r"sk-[A-Za-z0-9._\-]{10,}", ascii_only)
    if match:
        return match.group(0)

    if not text:
        raise ValueError("OPENAI_API_KEY is empty.")
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(
            "OPENAI_API_KEY contains non-ASCII characters. "
            "Open .env and re-paste the key with plain ASCII, e.g. "
            "OPENAI_API_KEY=sk-or-v1-..."
        ) from None
    return text


class LLMClient:
    """OpenAI 兼容的对话/向量封装；无 Key 或 MOCK_LLM 时走离线 mock。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: OpenAI | None = None
        if self.settings.use_live_llm:
            kwargs: dict[str, Any] = {
                "api_key": _clean_api_key(self.settings.openai_api_key),
                "base_url": _ascii_header(
                    self.settings.openai_base_url,
                    "https://openrouter.ai/api/v1",
                ),
            }
            if "openrouter.ai" in (self.settings.openai_base_url or ""):
                kwargs["default_headers"] = {
                    "HTTP-Referer": _ascii_header(
                        self.settings.openrouter_http_referer,
                        "http://127.0.0.1:8000",
                    ),
                    "X-Title": _ascii_header(
                        self.settings.openrouter_app_title,
                        "GrannyFlatBot",
                    ),
                }
            self._client = OpenAI(**kwargs)

    @property
    def live(self) -> bool:
        return self._client is not None

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        if not self._client:
            return self._mock_chat(messages)

        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
        }
        model_id = self.settings.openai_model.removeprefix("openai/")
        if model_id.startswith(("gpt-6-astra", "gpt-5.6-")):
            kwargs["reasoning_effort"] = "low"
            kwargs["max_completion_tokens"] = 4096
        else:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        if choice.finish_reason == "length" or not (choice.message.content or "").strip():
            raise ValueError("Model returned an incomplete or empty reply")
        return choice.message.content

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        raw = self.chat(
            messages,
            temperature=temperature,
            response_format={"type": "json_object"} if self.live else None,
        )
        return _extract_json(raw)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._client:
            return [_hash_embed(t) for t in texts]
        try:
            response = self._client.embeddings.create(
                model=self.settings.openai_embedding_model,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception:
            # OpenRouter / some models may not expose embeddings — keep retrieval working
            return [_hash_embed(t) for t in texts]

    def _mock_chat(self, messages: list[dict[str, str]]) -> str:
        """无 API 时的确定性演示回复。"""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        system = next((m["content"] for m in messages if m["role"] == "system"), "")

        # 翻译助手
        if "translator" in system.lower() or "翻译" in system:
            if "Chinese to natural Australian English" in system or "from Chinese to" in system:
                # 极简 mock：原样返回并加 [EN] 前缀，便于测试链路
                return f"[EN] {user}" if user else user
            if "译成" in system or "简体中文" in system:
                return f"【中文】{user}" if user else user

        if "JSON" in system or "json" in system.lower() or "requirement_patch" in system:
            return json.dumps(_mock_structured_turn(user, system))

        lower = user.lower()
        if any(k in lower for k in ("price", "quote", "cost", "how much")):
            reply = (
                "I'll review the model, delivery scope and site before confirming any estimate. "
                "Could you share the suburb and postcode, or the street address?"
            )
        elif "council" in lower:
            reply = (
                "Most new granny flats need a building-permit pathway. Planning rules are "
                "site-specific — share the suburb or address and we can check the right council pathway."
            )
        elif "kit" in lower or "turnkey" in lower:
            reply = (
                "We offer kit (supply only), managed_build (project-managed), and "
                "turnkey (handover-ready, site-dependent). Which delivery type interests you?"
            )
        else:
            reply = (
                "Happy to help with granny flat planning in Australia. "
                "I can answer from our product notes. What state is the property in, "
                "and are you thinking studio, 1-bed, 2-bed, or 3-bed (A80)?"
            )
        return reply


def _mock_structured_turn(user: str, system: str) -> dict[str, Any]:
    lower = user.lower()
    known = _parse_known_requirements(system)
    patch: dict[str, Any] = {}

    for state in ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"):
        if re.search(rf"\b{state.lower()}\b", lower) or state.lower() in lower:
            patch["state"] = state
            break
    if "sydney" in lower or "nsw" in lower:
        patch.setdefault("state", "NSW")
    if "melbourne" in lower or "victoria" in lower:
        patch.setdefault("state", "VIC")
    if "brisbane" in lower or "queensland" in lower:
        patch.setdefault("state", "QLD")

    if "studio" in lower:
        patch["bedrooms"] = 0
        patch["bathrooms"] = 1
    elif re.search(r"\b3[\s-]?bed", lower):
        patch["bedrooms"] = 3
    elif re.search(r"\b2[\s-]?bed", lower):
        patch["bedrooms"] = 2
    elif re.search(r"\b1[\s-]?bed", lower):
        patch["bedrooms"] = 1

    if "turnkey" in lower:
        patch["delivery_type"] = "turnkey"
    elif "managed" in lower:
        patch["delivery_type"] = "managed_build"
    elif re.search(r"\bkit\b", lower):
        patch["delivery_type"] = "kit"

    m = re.search(r"(\d{2,3})\s*m2", lower)
    if m:
        patch["approx_floor_area_m2"] = float(m.group(1))

    name_m = re.search(r"(?:my name is|i am|i'm)\s+([A-Z][a-z]+)", user, re.I)
    if name_m:
        patch["contact_name"] = name_m.group(1)
    addr_m = re.search(
        r"\b(\d+\s+[A-Za-z]+(?:\s+[A-Za-z]+)?\s+(?:St|Street|Rd|Road|Ave|Avenue)\b[^,]*)",
        user,
        re.I,
    )
    if addr_m:
        patch["street_address"] = addr_m.group(1).strip()
    suburb_m = re.search(
        r"\bin\s+([A-Za-z][A-Za-z\s]{2,40}?)\s+(?:VIC|NSW|QLD)\b", user, re.I
    )
    if suburb_m and "melbourne" not in suburb_m.group(1).lower():
        patch["suburb_or_lga"] = suburb_m.group(1).strip()
    if "melbourne" in lower:
        patch.setdefault("suburb_or_lga", "Melbourne")

    if "include council" in lower or "council package" in lower:
        patch["include_council"] = True
    if "include design" in lower:
        patch["include_design"] = True
    if "foundation" in lower:
        patch["include_foundations"] = True
    if "services" in lower or "sewer" in lower or "utilities" in lower:
        patch["include_services"] = True

    merged = {**known, **patch}
    wants_quote = any(
        k in lower
        for k in ("quote", "price", "how much", "cost", "报价", "多少钱", "估价")
    )

    # Non-quote questions: answer or admit uncertainty — never force "which state?"
    if not wants_quote:
        topical = _mock_topical_reply(lower)
        if topical:
            return {
                "reply": topical,
                "requirement_patch": patch,
                "offer_quote": False,
                "escalate_to_human": False,
                "escalation_reason": None,
            }
        return {
            "reply": (
                "I'll double-check that point for your site and get back to you. "
            ),
            "requirement_patch": patch,
            "offer_quote": False,
            "escalate_to_human": True,
            "escalation_reason": "question outside mock knowledge / no confident answer",
        }

    missing_hint = ""
    if not merged.get("state") and not (
        merged.get("postcode") or merged.get("street_address") or merged.get("suburb_or_lga")
    ):
        missing_hint = (
            "Could you share the suburb and postcode, or the street address?"
        )
    elif merged.get("bedrooms") is None:
        missing_hint = "How many bedrooms (studio / 1 / 2 / 3)?"
    elif not merged.get("delivery_type"):
        missing_hint = "Do you prefer kit, managed_build, or turnkey?"

    if not missing_hint:
        reply = (
            "I'll review the selected model, delivery scope and site before confirming an estimate."
        )
        offer_quote = False
    else:
        reply = (
            "An estimate needs a model, scope and site review. "
            + missing_hint
        )
        offer_quote = False

    return {
        "reply": reply,
        "requirement_patch": patch,
        "offer_quote": offer_quote,
        "escalate_to_human": True,
        "escalation_reason": "estimate requires site and scope review",
    }


def _parse_known_requirements(system: str) -> dict[str, Any]:
    """Read already-collected fields from the system prompt JSON block."""
    match = re.search(
        r"## Current requirements JSON\s*(\{.*?\})\s*(?:##|\Z)",
        system,
        re.DOTALL,
    )
    if not match:
        # Fallback: any JSON object in system that looks like requirements
        match = re.search(
            r'(\{\s*"(?:state|bedrooms|delivery_type)"[\s\S]*?\})',
            system,
        )
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _mock_topical_reply(lower: str) -> str | None:
    """Keyword answers for common non-quote questions (offline / API-fail path)."""
    if any(k in lower for k in ("council", "permit", "规划", "审批")) or re.search(r"\b(?:da|cdc)\b", lower):
        return (
            "Most new granny flats need a building-permit pathway. Planning rules "
            "differ by state and overlays. In Victoria, Small Second Dwelling rules "
            "often discuss ~60 m² GFA and may skip planning permit on many standard "
            "lots ≥300 m² without problematic overlays — but building permit is still "
            "required. Eligibility is site-specific; share your suburb and I'll confirm "
            "what usually applies for lots like yours."
        )
    if any(k in lower for k in ("ship", "transport", "freight", "海运", "运输", "吊装", "crane")):
        return (
            "Modules are factory-built in China, shipped by sea to ports such as "
            "Melbourne/Sydney/Brisbane/Perth, then trucked to site and crane-lifted "
            "onto a prepared slab. Exact logistics need a freight quote for your suburb."
        )
    if any(k in lower for k in ("kit", "turnkey", "managed", "交付", "全包")):
        return (
            "We offer kit (module supply focus), managed_build (coordinated local install), "
            "and turnkey (broader ready-to-occupy path, still site-dependent). "
            "Which delivery type are you considering?"
        )
    if any(k in lower for k in ("asbestos", "石棉", "engineered stone", "人造石")):
        return (
            "Import compliance is strict: asbestos-free evidence is critical, and "
            "engineered stone benchtops must not be specified. Share your suburb and "
            "I'll confirm the compliance checklist for your project."
        )
    if any(k in lower for k in ("warranty", "质保", "保修")):
        return (
            "Typically the local builder handles construction warranty service; "
            "Pukai warrants the supplied modules/materials. Exact terms sit in the "
            "builder contract — tell me your suburb and I'll confirm how we usually "
            "structure that in your area."
        )
    if any(
        k in lower
        for k in (
            "builder",
            "construction team",
            "local team",
            "install team",
            "施工",
            "施工队",
            "本地团队",
            "安装队",
        )
    ):
        return (
            "Yes — we work with licensed local Australian builders. "
            "Pukai supplies the modules; a local builder typically holds the "
            "construction contract, manages site works (slab, crane, joining, "
            "services), and helps with Occupancy Permit. Coverage is arranged "
            "by region (e.g. Greater Sydney / NSW) — I can connect you with "
            "the local partner for your suburb."
        )
    if any(k in lower for k in ("hello", "hi ", "hey", "你好", "您好")):
        return (
            "Hi — I can help with Australian modular granny flats: products, approvals, "
            "and next steps on site. What would you like to know?"
        )
    return None


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError(f"LLM did not return JSON object: {raw[:200]}")


def _hash_embed(text: str, dim: int = 64) -> list[float]:
    """Cheap bag-of-words embedding for offline retrieval."""
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        return vec
    for tok in tokens:
        h = hash(tok)
        vec[h % dim] += 1.0
        vec[(h // dim) % dim] += 0.5
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]
