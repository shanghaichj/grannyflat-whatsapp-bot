"""Local, auditable analysis for the separately stored legacy contacts.

The configured LLM is used only to turn an administrator's instruction into a
small declarative plan.  Contact names, phone numbers and messages never leave
the server through this module.  Matching and conservative enrichment happen
locally against the legacy ``SessionStore``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from grannyflat_bot.llm.client import LLMClient

logger = logging.getLogger(__name__)


MAX_INSTRUCTION_LENGTH = 800
MAX_SCOPE_IDS = 1_000
MAX_CONDITIONS = 12
MAX_TAG_LENGTH = 48
MAX_CONDITION_LENGTH = 120


class AnalysisCondition(BaseModel):
    """One safe condition generated from an administrator instruction."""

    model_config = ConfigDict(extra="ignore")

    field: Literal["inbound_messages", "phone", "name"]
    operator: Literal["contains", "not_contains", "equals", "starts_with", "ends_with"] = "contains"
    value: str = Field(min_length=1, max_length=MAX_CONDITION_LENGTH)

    @field_validator("value")
    @classmethod
    def clean_value(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("condition value cannot be empty")
        return cleaned


class AnalysisPlan(BaseModel):
    """Validated local rule; it deliberately cannot contain SQL or regex."""

    model_config = ConfigDict(extra="ignore")

    target_tag: str | None = Field(default=None, max_length=MAX_TAG_LENGTH)
    logic: Literal["all", "any"] = "all"
    conditions: list[AnalysisCondition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    apply_to_all: bool = False

    @field_validator("target_tag")
    @classmethod
    def clean_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            return None
        if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
            raise ValueError("tag contains control characters")
        return cleaned


class PlanUnavailable(RuntimeError):
    """Raised when the configured live model cannot parse an instruction."""


PLAN_SYSTEM_PROMPT = """You convert a trusted administrator instruction into a safe local contact-filter plan.
Return one JSON object with exactly this shape:
{
  "target_tag": "a short tag to add, or null",
  "logic": "all" or "any",
  "apply_to_all": false,
  "conditions": [
    {"field": "inbound_messages" or "phone" or "name",
     "operator": "contains" or "not_contains" or "equals" or "starts_with" or "ends_with",
     "value": "literal text"}
  ]
}

Rules:
- Parse only the administrator instruction below. No contact records are supplied.
- Use literal values only. Never produce SQL, regular expressions, scripts, or executable expressions.
- Use at most 12 conditions. Keep every value short and literal.
- Set apply_to_all=true only when the administrator explicitly asks to tag every contact in the chosen scope.
- If a tag is requested but there are no conditions and apply_to_all is false, nobody will receive that tag.
- If the instruction asks only to infer location/state or detect builders, use target_tag=null and no conditions.
- If the requested tag is builder, output target_tag="builder". The server applies a separate strict
  self-identification test; a bare occurrence of the word builder will never be sufficient.
- Do not invent a tag when the instruction does not request one.
"""

_EXPLICIT_ALL_RE = re.compile(
    r"(?:\b(?:all|every)\s+(?:contact|contacts|customer|customers|record|records)\b"
    r"|\btag\s+(?:them\s+)?all\b"
    r"|(?:所有|全部|每个|每一位)(?:联系人|客户|记录)"
    r"|(?:全部|全都)(?:加|添加|标记)(?:上)?标签)",
    re.IGNORECASE,
)


def parse_instruction(instruction: str, *, client: LLMClient | None = None) -> AnalysisPlan:
    """Parse one admin instruction without including any customer data."""
    text = instruction.strip()
    if not text or len(text) > MAX_INSTRUCTION_LENGTH:
        raise ValueError("instruction must contain 1 to 800 characters")
    llm = client or LLMClient()
    if not llm.live:
        raise PlanUnavailable("A live LLM is required to parse analysis instructions")
    try:
        raw = llm.chat_json(
            [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        plan = AnalysisPlan.model_validate(raw)
        # A model cannot authorize a mass update by itself. The admin text must
        # contain an explicit all-contact phrase as a second independent gate.
        if plan.apply_to_all and not _EXPLICIT_ALL_RE.search(text):
            plan = plan.model_copy(update={"apply_to_all": False})
        return plan
    except PlanUnavailable:
        raise
    except Exception as exc:
        raise PlanUnavailable("The analysis instruction could not be parsed") from exc


def _fold(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _compact(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _inbound_messages(session: Any) -> list[str]:
    """Return de-duplicated customer messages only, preserving first occurrence."""
    seen: set[str] = set()
    messages: list[str] = []
    for item in list(getattr(session, "history", None) or []):
        role = _fold(item.get("role")) if isinstance(item, dict) else ""
        if role not in {"user", "customer", "inbound", "incoming"}:
            continue
        content = _compact(item.get("content") if isinstance(item, dict) else "")
        if not content:
            continue
        key = _fold(content)
        if key in seen:
            continue
        seen.add(key)
        messages.append(content)
    return messages


def _contact_name(session: Any) -> str:
    requirements = getattr(session, "requirements", None)
    values = [
        getattr(session, "display_name", None),
        getattr(session, "whatsapp_profile_name", None),
        getattr(requirements, "contact_name", None),
    ]
    return "\n".join(dict.fromkeys(_compact(v) for v in values if _compact(v)))


def _contact_phone(session: Any) -> str:
    requirements = getattr(session, "requirements", None)
    values = [
        getattr(session, "whatsapp_phone", None),
        getattr(requirements, "contact_phone", None),
    ]
    session_id = str(getattr(session, "session_id", "") or "")
    if session_id.startswith("wa-"):
        values.append(session_id[3:])
    return "\n".join(dict.fromkeys(_compact(v) for v in values if _compact(v)))


def _phone_value(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits if digits else _fold(value)


def _condition_matches(condition: AnalysisCondition, fields: dict[str, str]) -> bool:
    haystack = fields[condition.field]
    needle = condition.value
    if condition.field == "phone":
        left = _phone_value(haystack)
        right = _phone_value(needle)
    else:
        left = _fold(haystack)
        right = _fold(needle)
    if condition.operator == "contains":
        return right in left
    if condition.operator == "not_contains":
        return right not in left
    if condition.operator == "equals":
        return left == right
    if condition.operator == "starts_with":
        return left.startswith(right)
    if condition.operator == "ends_with":
        return left.endswith(right)
    return False


_SELF_BUILDER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:i\s*(?:am|'m)|we\s+are)\s+(?:an?\s+)?(?:licensed\s+|registered\s+|home\s+|modular\s+|granny\s+flat\s+){0,3}(?:builder|builders|building\s+contractor|construction\s+contractor)\b",
        r"\b(?:i|we)\s+(?:work|operate|trade)\s+as\s+(?:an?\s+)?(?:builder|building\s+contractor|construction\s+contractor)\b",
        r"\b(?:i|we)\s+(?:run|own)\s+(?:a\s+)?(?:building|construction)\s+(?:company|business)\b",
        r"\b(?:my|our)\s+(?:building|construction)\s+(?:company|business)\b",
        r"(?:我是|我们是)[^。！!？?\n]{0,16}(?:建筑商|建造商|承建商|施工方)",
        r"(?:我|我们)(?:经营|开|拥有)[^。！!？?\n]{0,12}(?:建筑|建造|施工)(?:公司|企业)",
    )
)


def is_high_confidence_builder(messages: Sequence[str]) -> bool:
    """Require customer self-identification; a bare builder mention is insufficient."""
    return any(pattern.search(message) for message in messages for pattern in _SELF_BUILDER_PATTERNS)


STATE_NAMES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), state)
    for pattern, state in (
        (r"\bNSW\b|\bNew\s+South\s+Wales\b|新南威尔士|新州", "NSW"),
        (r"\bVIC\b|\bVictoria\b|维多利亚|维州", "VIC"),
        (r"\bQLD\b|\bQueensland\b|昆士兰|昆州", "QLD"),
        (r"\bSA\b|\bSouth\s+Australia\b|南澳", "SA"),
        (r"\bWA\b|\bWestern\s+Australia\b|西澳", "WA"),
        (r"\bTAS\b|\bTasmania\b|塔斯马尼亚|塔州", "TAS"),
        (r"\bACT\b|\bAustralian\s+Capital\s+Territory\b|首都领地", "ACT"),
        (r"\bNT\b|\bNorthern\s+Territory\b|北领地", "NT"),
    )
)

CITY_STATES: dict[str, tuple[str, str]] = {
    "sydney": ("Sydney", "NSW"),
    "melbourne": ("Melbourne", "VIC"),
    "brisbane": ("Brisbane", "QLD"),
    "gold coast": ("Gold Coast", "QLD"),
    "sunshine coast": ("Sunshine Coast", "QLD"),
    "adelaide": ("Adelaide", "SA"),
    "perth": ("Perth", "WA"),
    "hobart": ("Hobart", "TAS"),
    "canberra": ("Canberra", "ACT"),
    "darwin": ("Darwin", "NT"),
}

_POSTCODE_EXPLICIT_RE = re.compile(r"\b(?:post\s*code|postcode|zip)\s*(?:is|:)?\s*(\d{4})\b", re.I)
_POSTCODE_ONLY_RE = re.compile(r"^\s*(\d{4})\s*$")
_STATE_POSTCODE_RE = re.compile(r"\b(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)\s+(\d{4})\b", re.I)
_STREET_RE = re.compile(
    r"\b((?:Unit\s+\d+[A-Za-z]?[,\s]+)?\d+(?:/\d+)?[A-Za-z]?\s+"
    r"[A-Za-z0-9][A-Za-z0-9' .-]{1,50}?\s+"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|Way|"
    r"Parade|Pde|Lane|Ln|Boulevard|Blvd|Crescent|Cres))\b",
    re.I,
)
_STATE_CODE_TEXT = r"NSW|VIC|QLD|SA|WA|TAS|ACT|NT"
_SUBURB_STATE_RE = re.compile(
    rf"^\s*([A-Za-z][A-Za-z' -]{{1,32}}?)\s*,?\s+"
    rf"({_STATE_CODE_TEXT})(?:\s+(\d{{4}}))?\s*$",
    re.I,
)
_TRAILING_SUBURB_STATE_RE = re.compile(
    rf"\b(?:in|at)\s+([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){{0,3}})"
    rf"\s*,?\s+({_STATE_CODE_TEXT})(?:\s+(\d{{4}}))?\s*[.!?]*\s*$",
    re.I,
)
_STREET_LOCALITY_RE = re.compile(
    r"\b((?:Unit\s+\d+[A-Za-z]?[,]?\s+)?\d+(?:/\d+)?[A-Za-z]?\s+"
    r"[A-Za-z0-9][A-Za-z0-9' .-]{1,50}?\s+"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|Way|"
    r"Parade|Pde|Lane|Ln|Boulevard|Blvd|Crescent|Cres))"
    rf"\s*,?\s+([A-Za-z][A-Za-z' -]{{1,32}}?)\s*,?\s+"
    rf"({_STATE_CODE_TEXT})(?:\s+(\d{{4}}))?\b",
    re.I,
)
_LOCATION_CONTEXT_RES = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:i\s+am|i'm|we\s+are|we're)\s+(?:currently\s+)?"
        r"(?:(?:based|located|living)\s+)?(?:in|at)\s+([^\n.!?]+)",
        r"\b(?:my|our|the)\s+(?:property|site|project|address|location)\s+"
        r"(?:(?:is\s+)?(?:based|located)\s+(?:in|at)|(?:is\s+)?(?:in|at)|is|:)\s+"
        r"([^\n.!?]+)",
        r"\b(?:site\s+address|property\s+address|project\s+address|address|location|suburb|city)"
        r"\s*(?:is|:)\s*([^\n.!?]+)",
        r"\b(?:based|located)\s+(?:in|at)\s+([^\n.!?]+)",
    )
)
_PURE_STATE_RE = re.compile(
    r"^\s*(NSW|New\s+South\s+Wales|VIC|Victoria|QLD|Queensland|"
    r"SA|South\s+Australia|WA|Western\s+Australia|TAS|Tasmania|"
    r"ACT|Australian\s+Capital\s+Territory|NT|Northern\s+Territory)"
    r"(?:\s+(\d{4}))?\s*[.!]?\s*$",
    re.I,
)
_GENERIC_LOCATION_WORDS_RE = re.compile(
    r"\b(?:granny\s+flat|property|site|project|address|house|home|land|"
    r"postcode|zip|builder|deliver|delivery|install|installation|service|services)\b",
    re.I,
)
# 句尾"…, Sydney"/"in Sydney"这类大城市提及也视为明确位置事实
# （如客户回复 "Rental, Sydney"）：大城市名无歧义，可安全映射到州。
_TRAILING_CITY_RES: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (
        re.compile(
            rf"(?:,|\bin\b|\bat\b)\s*{re.escape(city_name)}\s*[.!?]*\s*$", re.I
        ),
        city_name,
        state,
    )
    for city_name, state in CITY_STATES.values()
)
_STREET_SUFFIX_RE = re.compile(
    r"\b(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|Way|"
    r"Parade|Pde|Lane|Ln|Boulevard|Blvd|Crescent|Cres)\b",
    re.I,
)


def _state_for_postcode(postcode: str) -> str | None:
    if not re.fullmatch(r"\d{4}", postcode):
        return None
    value = int(postcode)
    ranges = (
        (200, 299, "ACT"), (800, 999, "NT"),
        (1000, 2599, "NSW"), (2600, 2618, "ACT"),
        (2619, 2899, "NSW"), (2900, 2920, "ACT"), (2921, 2999, "NSW"),
        (3000, 3999, "VIC"), (4000, 4999, "QLD"),
        (5000, 5999, "SA"), (6000, 6999, "WA"),
        (7000, 7999, "TAS"), (8000, 8999, "VIC"), (9000, 9999, "QLD"),
    )
    return next((state for low, high, state in ranges if low <= value <= high), None)


@dataclass(frozen=True)
class LocationCandidate:
    state: str | None = None
    city: str | None = None
    postcode: str | None = None
    street_address: str | None = None


def _state_matches(text: str) -> set[str]:
    """Return state names found inside an already trusted location fragment."""
    return {state for pattern, state in STATE_NAMES if pattern.search(text)}


def _clean_suburb(value: str) -> str | None:
    """Validate a suburb/LGA captured from a trusted location expression."""
    cleaned = " ".join(value.strip(" ,").split())
    if not cleaned or not 1 <= len(cleaned.split()) <= 4:
        return None
    if any(ch.isdigit() for ch in cleaned):
        return None
    if _GENERIC_LOCATION_WORDS_RE.search(cleaned) or _STREET_SUFFIX_RE.search(cleaned):
        return None
    return cleaned.title()


def _location_fragments(message: str) -> list[str]:
    """Extract only self-location/address fragments or a pure location reply."""
    fragments: list[str] = []
    for pattern in _LOCATION_CONTEXT_RES:
        for match in pattern.finditer(message):
            value = " ".join(match.group(1).strip(" ,").split())
            if value:
                fragments.append(value)

    stripped = message.strip()
    pure_state = _PURE_STATE_RE.fullmatch(stripped)
    pure_suburb_state = _SUBURB_STATE_RE.fullmatch(stripped.strip(".!? "))
    # A complete street address supplied as the whole reply is also explicit.
    pure_address = bool(_STREET_RE.match(stripped))
    trusted_city_only = any(
        re.fullmatch(rf"\s*{re.escape(city)}\s*[.!]?\s*", stripped, re.I)
        for city in CITY_STATES
    )
    if (
        pure_state
        or (pure_suburb_state and _clean_suburb(pure_suburb_state.group(1)))
        or pure_address
        or trusted_city_only
    ):
        fragments.append(stripped.strip(".!? "))

    # A final "in/at <suburb> STATE" phrase is sufficiently specific even in a
    # longer project sentence. Generic product phrases such as "in granny flat,
    # NSW" fail `_clean_suburb` and therefore do not make the fragment trusted.
    trailing = _TRAILING_SUBURB_STATE_RE.search(stripped)
    if trailing and _clean_suburb(trailing.group(1)):
        value = " ".join(
            part for part in (trailing.group(1), trailing.group(2), trailing.group(3))
            if part
        )
        fragments.append(value)

    # A trailing major-city mention ("Rental, Sydney" / "based in Melbourne") is
    # an explicit location fact on its own — no suburb/state pairing required.
    for pattern, city_name, _state in _TRAILING_CITY_RES:
        if pattern.search(stripped):
            fragments.append(city_name)
            break

    # Keep order while avoiding duplicate work when context and pure-reply rules overlap.
    return list(dict.fromkeys(fragments))


def infer_location(messages: Sequence[str]) -> LocationCandidate:
    """Extract only unambiguous location facts from inbound messages."""
    states: set[str] = set()
    cities: set[str] = set()
    postcodes: set[str] = set()
    streets: set[str] = set()

    for message in messages:
        # A specifically labelled postcode is safe even when it appears in a sentence.
        explicit_postcode = _POSTCODE_EXPLICIT_RE.search(message)
        if explicit_postcode:
            postcode = explicit_postcode.group(1)
            postcodes.add(postcode)
            postcode_state = _state_for_postcode(postcode)
            if postcode_state:
                states.add(postcode_state)

        for fragment in _location_fragments(message):
            street_locality = _STREET_LOCALITY_RE.search(fragment)
            street = _STREET_RE.search(fragment)
            invalid_suburb_state = False
            if street:
                streets.add(" ".join(street.group(1).split()))

            if street_locality:
                suburb = _clean_suburb(street_locality.group(2))
                if suburb:
                    cities.add(suburb)
                    states.add(street_locality.group(3).upper())
                    if street_locality.group(4):
                        postcodes.add(street_locality.group(4))
            else:
                suburb_match = _SUBURB_STATE_RE.fullmatch(fragment.strip(".!? "))
                if suburb_match and not street:
                    suburb = _clean_suburb(suburb_match.group(1))
                    if suburb:
                        cities.add(suburb)
                        states.add(suburb_match.group(2).upper())
                        if suburb_match.group(3):
                            postcodes.add(suburb_match.group(3))
                    else:
                        invalid_suburb_state = True

            if invalid_suburb_state:
                continue

            for key, (city, state) in CITY_STATES.items():
                if re.search(rf"\b{re.escape(key)}\b", fragment, re.I):
                    cities.add(city)
                    states.add(state)

            states.update(_state_matches(fragment))

            postcode_match = (
                _STATE_POSTCODE_RE.search(fragment)
                or _POSTCODE_ONLY_RE.fullmatch(fragment.strip())
            )
            if postcode_match:
                postcode = postcode_match.group(1)
                postcodes.add(postcode)
                postcode_state = _state_for_postcode(postcode)
                if postcode_state:
                    states.add(postcode_state)

            # A self-described location may name an unlisted suburb without a state.
            if not street and not street_locality and not _state_matches(fragment):
                suburb = _clean_suburb(fragment.strip(".!? "))
                if suburb:
                    cities.add(suburb)

    # Conflicting facts are intentionally left blank rather than guessed.
    return LocationCandidate(
        state=next(iter(states)) if len(states) == 1 else None,
        city=next(iter(cities)) if len(cities) == 1 else None,
        postcode=next(iter(postcodes)) if len(postcodes) == 1 else None,
        street_address=next(iter(streets)) if len(streets) == 1 else None,
    )


def _fields_for(session: Any, messages: Sequence[str]) -> dict[str, str]:
    return {
        "inbound_messages": "\n".join(messages),
        "phone": _contact_phone(session),
        "name": _contact_name(session),
    }


def _matches_plan(
    plan: AnalysisPlan,
    *,
    fields: dict[str, str],
    strict_builder: bool,
) -> bool:
    if not plan.target_tag:
        return False
    outcomes = [_condition_matches(condition, fields) for condition in plan.conditions]
    matched = (
        all(outcomes) if plan.logic == "all" else any(outcomes)
    ) if outcomes else plan.apply_to_all
    # A builder tag always requires self-identification, even if the parsed plan
    # contains a weak condition such as messages contains "builder".
    if _fold(plan.target_tag) == "builder":
        matched = strict_builder and (matched or not outcomes)
    return matched


def _has_tag(tags: Sequence[str], tag: str, *, exact_builder: bool = False) -> bool:
    if exact_builder:
        return any(existing == "builder" for existing in tags)
    wanted = _fold(tag)
    return any(_fold(existing) == wanted for existing in tags)


def _add_tag(session: Any, tag: str) -> bool:
    tags = list(getattr(session, "tags", None) or [])
    exact_builder = tag == "builder"
    if _has_tag(tags, tag, exact_builder=exact_builder):
        return False
    tags.append(tag)
    session.tags = tags
    return True


def _populate_location(session: Any, candidate: LocationCandidate) -> bool:
    requirements = getattr(session, "requirements", None)
    if requirements is None:
        return False
    patch: dict[str, Any] = {}
    changed = False

    existing_state = getattr(session, "state", None) or getattr(requirements, "state", None)
    existing_city = getattr(session, "city", None) or getattr(requirements, "suburb_or_lga", None)
    existing_postcode = getattr(session, "postcode", None) or getattr(requirements, "postcode", None)
    existing_address = getattr(session, "address", None)
    existing_street = getattr(requirements, "street_address", None)

    if not existing_state and candidate.state:
        session.state = candidate.state
        patch["state"] = candidate.state
        changed = True
    if not existing_city and candidate.city:
        session.city = candidate.city
        patch["suburb_or_lga"] = candidate.city
        changed = True
    if not existing_postcode and candidate.postcode:
        session.postcode = candidate.postcode
        patch["postcode"] = candidate.postcode
        changed = True
    if not existing_street and candidate.street_address:
        patch["street_address"] = candidate.street_address
        changed = True

    if patch:
        session.requirements = requirements.apply_patch(
            patch,
            source="legacy_analysis",
            confidence=0.95,
        )
    if not existing_address:
        resolved = session.requirements
        street = _compact(getattr(resolved, "street_address", None))
        if street:
            state_value = getattr(session, "state", None) or getattr(resolved, "state", None)
            state = _compact(getattr(state_value, "value", state_value))
            city = _compact(
                getattr(session, "city", None) or getattr(resolved, "suburb_or_lga", None)
            )
            postcode = _compact(
                getattr(session, "postcode", None) or getattr(resolved, "postcode", None)
            )
            locality = " ".join(part for part in (city, state, postcode) if part)
            session.address = f"{street}, {locality}" if locality else street
            changed = True
    return changed


def _mark_builder(session: Any, strict_builder: bool) -> tuple[bool, bool]:
    """Infer customer identity only; free-form tags are always staff-managed."""
    if not strict_builder:
        return False, False
    requirements = getattr(session, "requirements", None)
    current = _fold(getattr(session, "contact_type", None))
    req_current = _fold(getattr(requirements, "contact_type", None)) if requirements is not None else ""
    type_set = False
    if current in {"", "unknown"} and req_current in {"", "unknown"}:
        session.contact_type = "builder"
        if requirements is not None:
            session.requirements = requirements.apply_patch(
                {"contact_type": "builder"},
                source="legacy_analysis",
                confidence=0.98,
            )
        type_set = True
    return False, type_set


def _select_sessions(store: Any, scope: str, session_ids: Sequence[str]) -> list[Any]:
    sessions = [
        session for session in store.list_all()
        if not str(getattr(session, "session_id", "")).startswith("teach-")
    ]
    if scope == "all":
        return sessions
    wanted = {str(session_id) for session_id in session_ids if str(session_id).strip()}
    return [session for session in sessions if str(getattr(session, "session_id", "")) in wanted]


@dataclass(frozen=True)
class AnalysisResult:
    scanned: int
    matched: int
    tagged: int
    location_updated: int
    builder_tagged: int
    updated: int
    skipped: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "tagged": self.tagged,
            "location_updated": self.location_updated,
            "builder_tagged": self.builder_tagged,
            "updated": self.updated,
            "skipped": self.skipped,
            "message": self.message,
        }


PlanParser = Callable[[str], AnalysisPlan]


def analyze_legacy_contacts(
    store: Any,
    *,
    instruction: str,
    scope: Literal["all", "filtered", "selected"] = "all",
    session_ids: Sequence[str] = (),
    plan_parser: PlanParser | None = None,
) -> AnalysisResult:
    """Run one local analysis and persist only changed legacy sessions."""
    parser = plan_parser or parse_instruction
    plan = parser(instruction)
    sessions = _select_sessions(store, scope, session_ids)

    matched = 0
    tagged = 0
    location_updated = 0
    builder_tagged = 0
    updated = 0

    for session in sessions:
        messages = _inbound_messages(session)
        strict_builder = is_high_confidence_builder(messages)
        changed = False

        if _populate_location(session, infer_location(messages)):
            location_updated += 1
            changed = True

        builder_added, builder_type_set = _mark_builder(session, strict_builder)
        if builder_added:
            builder_tagged += 1
        if builder_added or builder_type_set:
            changed = True

        fields = _fields_for(session, messages)
        if _matches_plan(plan, fields=fields, strict_builder=strict_builder):
            matched += 1

        if changed:
            # Preserve the original imported time and therefore contact ordering.
            store.save(session, touch=False)
            updated += 1

    scanned = len(sessions)
    return AnalysisResult(
        scanned=scanned,
        matched=matched,
        tagged=tagged,
        location_updated=location_updated,
        builder_tagged=builder_tagged,
        updated=updated,
        skipped=scanned - updated,
        message=(
            f"分析完成：检查 {scanned} 个老号码联系人，更新 {updated} 个。"
            if scanned
            else "分析完成：当前范围内没有联系人。"
        ),
    )


TAG_PRESETS: tuple[dict[str, str], ...] = ()


def distinct_tags(store: Any) -> list[str]:
    """Return existing tags without exposing any other contact data."""
    unique: dict[str, str] = {}
    for session in store.list_all():
        if str(getattr(session, "session_id", "")).startswith("teach-"):
            continue
        for value in list(getattr(session, "tags", None) or []):
            tag = " ".join(str(value).strip().split())
            if tag:
                unique.setdefault(_fold(tag), tag)
    return sorted(unique.values(), key=_fold)


# ---------------------------------------------------------------------------
# AI 历史聊天分析（固定流程）：更新需求字段 + 资格评分 + AI 销售摘要。
#
# 这条路径取代了原来的“AI 指令分析”（自然语言指令 -> 打标签）。
# 这里不再让管理员写指令：流程是固定的——本地位置/身份提取 ->
# LLM 从真实历史聊天提取结构化需求字段（只填空，绝不覆盖）-> 资格评分
# -> 冷热度 -> AI 销售摘要 -> 建议下一步行动与跟进时间。
# 全程只写老号码独立数据库，不发送任何 WhatsApp 消息。
# ---------------------------------------------------------------------------

AI_EXTRACTION_SYSTEM_PROMPT = """You extract structured granny-flat requirements from a WhatsApp sales conversation.
Return ONE JSON object shaped exactly like:
{"patch": {"<field>": <value>, ...}}
Allowed fields and types (omit a field when the conversation does not state it):
- state: one of NSW, VIC, QLD, SA, WA, TAS, ACT, NT
- suburb_or_lga: string
- postcode: 4-digit string
- street_address: string
- bedrooms: integer (0 means studio)
- bathrooms: number
- lot_size_m2: number
- delivery_type: one of "kit", "managed_build", "turnkey"
- timeline: string, e.g. "3 months"
- budget_aud_min: number
- budget_aud_max: number
- contact_name: string
- contact_phone: string
- contact_email: string
- contact_type: one of owner, builder, agent, consultant, designer
- usage_type: one of owner_occupier, rental, investment, builder
- unit_count: integer >= 1
Rules:
- Use only facts explicitly stated by the customer. Never invent budget, timeline or intent.
- An Australian city name stated by the customer implies its state (geography, not invention):
  e.g. "Sydney" means state=NSW and suburb_or_lga=Sydney; "Brisbane" means state=QLD.
- Usage words imply usage_type: "rental"/"rent" -> rental, "investment" -> investment,
  "live in"/"owner occupier" -> owner_occupier.
- Prefer the customer's own words for timeline/contact fields.
- If facts conflict between messages, omit the field.
- Output JSON only. No markdown, no explanation."""

_INT_FIELDS = {"bedrooms", "unit_count"}
_FLOAT_FIELDS = {"bathrooms", "lot_size_m2", "budget_aud_min", "budget_aud_max"}
_STATE_CODES = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"}
_DELIVERY_TYPES = {"kit", "managed_build", "turnkey"}
_CONTACT_TYPES = {"owner", "builder", "agent", "consultant", "designer"}
_USAGE_TYPES = {"owner_occupier", "rental", "investment", "builder"}
_MAX_HISTORY_MESSAGES = 30
_MAX_MESSAGE_CHARS = 800
_MAX_EXTRACT_CHARS = 9000


def _coerce_patch_value(field: str, value: Any) -> Any:
    """Validate and coerce one extracted value; return None when unusable."""
    if value is None:
        return None
    if field in _INT_FIELDS:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
    if field in _FLOAT_FIELDS:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
    if field == "state":
        code = str(value).strip().upper()
        return code if code in _STATE_CODES else None
    if field == "delivery_type":
        code = str(value).strip().lower()
        return code if code in _DELIVERY_TYPES else None
    if field == "contact_type":
        code = str(value).strip().lower()
        return code if code in _CONTACT_TYPES else None
    if field == "usage_type":
        code = str(value).strip().lower()
        return code if code in _USAGE_TYPES else None
    if field == "postcode":
        code = str(value).strip()
        return code if re.fullmatch(r"\d{4}", code) else None
    text = " ".join(str(value).split())
    return text[:200] or None


def extract_history_patch(
    messages: Sequence[str], *, client: Any
) -> tuple[dict[str, Any], str | None]:
    """Ask the configured LLM for a structured patch.

    Returns (patch, error). error is None on success; when the LLM call fails
    the patch is empty and error explains why, so batch analysis can surface
    configuration problems (e.g. model region restrictions) instead of
    silently producing nothing.
    """
    if client is None or not getattr(client, "live", False):
        return {}, "LLM 未配置"
    if not messages:
        return {}, None
    transcript = "\n".join(
        f"customer: {message[:_MAX_MESSAGE_CHARS]}"
        for message in list(messages)[-_MAX_HISTORY_MESSAGES:]
    )[:_MAX_EXTRACT_CHARS]
    try:
        raw = client.chat_json(
            [
                {"role": "system", "content": AI_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Conversation:\n{transcript}"},
            ],
            temperature=0.0,
        )
        patch_raw = raw.get("patch") if isinstance(raw, dict) else None
        if not isinstance(patch_raw, dict):
            logger.warning("LLM history patch returned non-dict payload")
            return {}, "LLM 返回格式异常"
    except Exception as exc:
        logger.warning("LLM history extraction failed: %s", exc)
        return {}, f"LLM 调用失败：{exc}"[:200]
    patch: dict[str, Any] = {}
    for field, value in patch_raw.items():
        coerced = _coerce_patch_value(str(field), value)
        if coerced is not None:
            patch[str(field)] = coerced
    return patch, None


def _fill_only_patch(requirements: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields that are currently blank; never overwrite existing facts."""
    if not patch:
        return {}
    try:
        current = requirements.model_dump()
    except Exception:
        return {}
    return {
        key: value
        for key, value in patch.items()
        if current.get(key) is None or current.get(key) == ""
    }


@dataclass(frozen=True)
class LegacyHistoryResult:
    scanned: int
    updated: int
    fields_updated: int
    location_updated: int
    builder_marked: int
    qualified: int
    summarized: int
    llm_errors: int
    skipped: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "updated": self.updated,
            "fields_updated": self.fields_updated,
            "location_updated": self.location_updated,
            "builder_marked": self.builder_marked,
            "qualified": self.qualified,
            "summarized": self.summarized,
            "llm_errors": self.llm_errors,
            "skipped": self.skipped,
            "message": self.message,
        }


def _display_order(sessions: Sequence[Any]) -> list[Any]:
    """按通讯录显示顺序排序：有聊天记录的在前，其余按 sort_key 降序。

    与工作台前端 sortContactsByChatTime / 后端联系人列表排序规则保持一致，
    使 AI 分析按用户在页面上看到的 1→N 顺序执行。
    """
    def _key(session: Any) -> tuple[int, str]:
        has_messages = 1 if _inbound_messages(session) else 0
        sort_key_fn = getattr(session, "sort_key", None)
        sort_key = str(sort_key_fn() if callable(sort_key_fn) else "")
        return (has_messages, sort_key)

    return sorted(sessions, key=_key, reverse=True)


def analyze_legacy_history(
    store: Any,
    *,
    scope: Literal["all", "filtered", "selected"] = "all",
    session_ids: Sequence[str] = (),
    llm: Any = None,
    progress: Callable[[int, int], None] | None = None,
) -> LegacyHistoryResult:
    """Run the fixed AI pipeline over historical chats of the isolated legacy store."""
    # 延迟导入避免循环依赖（sales_intelligence 依赖 contacts，contacts 不依赖本模块）。
    from grannyflat_bot.sales_intelligence import (
        default_next_action,
        follow_up_datetime,
        generate_ai_summary,
        qualify_lead,
        recommended_follow_up_hours,
    )

    sessions = _select_sessions(store, scope, session_ids)
    if scope in {"all", "filtered"}:
        # “全部/当前筛选结果”按通讯录显示顺序分析（1→N）。
        sessions = _display_order(sessions)
    total = len(sessions)
    if progress is not None:
        try:
            progress(0, total)
        except Exception:
            pass
    now = datetime.now(timezone.utc).isoformat()

    updated = 0
    fields_updated = 0
    location_updated = 0
    builder_marked = 0
    qualified = 0
    summarized = 0
    llm_errors = 0
    llm_error_sample = ""

    for index, session in enumerate(sessions):
        messages = _inbound_messages(session)
        changed = False

        # 1) 本地确定性提取：位置 + 建造商身份（原逻辑保留）。
        if _populate_location(session, infer_location(messages)):
            location_updated += 1
            changed = True
        _builder_added, builder_type_set = _mark_builder(
            session, is_high_confidence_builder(messages)
        )
        if builder_type_set:
            builder_marked += 1
            changed = True

        # 2) LLM 从真实历史聊天提取结构化需求字段（只填空，绝不覆盖）。
        raw_patch, extract_error = extract_history_patch(messages, client=llm)
        if extract_error:
            llm_errors += 1
            if not llm_error_sample:
                llm_error_sample = extract_error
        patch = _fill_only_patch(session.requirements, raw_patch)
        if patch:
            try:
                session.requirements = session.requirements.apply_patch(
                    patch,
                    source="ai_history_analysis",
                    confidence=0.8,
                )
                fields_updated += 1
                changed = True
            except Exception:
                # 单个会话的非法 patch 不影响整批分析。
                pass

        # 3) 资格评分 / 冷热度（尊重人工锁定）。
        stage = str(getattr(session, "sales_stage", None) or "new_lead")
        qualification = qualify_lead(
            session.requirements,
            history=session.history,
            sales_stage=stage,
            whatsapp_phone=getattr(session, "whatsapp_phone", None),
            contact_type=getattr(session, "contact_type", None),
        )
        session.qualification_score = qualification.score
        session.qualification_level = qualification.level
        session.qualification_missing = list(qualification.missing)
        if not getattr(session, "lead_temperature_manual", False):
            session.lead_temperature = qualification.temperature
        qualified += 1
        changed = True

        # 4) AI 销售摘要（模型不可用时自动退回确定性摘要）。
        summary_text, summary_source = generate_ai_summary(
            llm,
            requirements=session.requirements,
            history=session.history,
            sales_stage=stage,
        )
        session.ai_summary = summary_text
        session.ai_summary_source = summary_source
        session.ai_summary_updated_at = now
        summarized += 1
        changed = True

        # 5) 建议下一步行动与跟进时间（尊重人工锁定）。
        if not getattr(session, "next_action_manual", False):
            session.next_action = default_next_action(stage) or session.next_action
        if not getattr(session, "follow_up_due_manual", False):
            session.follow_up_due_at = follow_up_datetime(
                recommended_follow_up_hours(stage), sales_stage=stage
            )

        sync = getattr(session, "sync_contact_fields", None)
        if callable(sync):
            sync()

        if changed:
            store.save(session, touch=False)
            updated += 1

        if progress is not None:
            try:
                progress(index + 1, total)
            except Exception:
                pass

    scanned = len(sessions)
    message = (
        f"AI 分析完成：检查 {scanned} 个老号码联系人，更新 {updated} 个"
        f"（补充需求字段 {fields_updated} 个，生成摘要 {summarized} 个）。"
        if scanned
        else "AI 分析完成：当前范围内没有联系人。"
    )
    if llm_errors:
        message += (
            f" 警告：{llm_errors} 个联系人的 LLM 提取失败（如：{llm_error_sample}）"
            "——已仅使用本地规则提取，请检查 OPENAI_MODEL/网络配置。"
        )
    return LegacyHistoryResult(
        scanned=scanned,
        updated=updated,
        fields_updated=fields_updated,
        location_updated=location_updated,
        builder_marked=builder_marked,
        qualified=qualified,
        summarized=summarized,
        llm_errors=llm_errors,
        skipped=scanned - updated,
        message=message,
    )
