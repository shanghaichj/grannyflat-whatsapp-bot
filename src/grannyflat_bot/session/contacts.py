"""通讯录字段与分类（内部中文标签，API 用 code）。

显示名规则（当前）：
1. 客户姓名（问卷 / WhatsApp 资料名）
2. 没有姓名则用完整电话
3. 客户给出的地址仍自动写入通讯录 address 栏（不影响显示名）
"""

from __future__ import annotations

import re
from typing import Any

from grannyflat_bot.intake.schema import RequirementState

# 客户角色分类（客服可手动改；对话中可自动识别）
CONTACT_TYPES: dict[str, str] = {
    "unknown": "未分类",
    "owner": "业主",
    "builder": "建造商",
    "agent": "代理",
    "consultant": "顾问",
    "designer": "设计师",
    "investor": "投资客",
    "distributor": "经销商",
    "other": "其他",
}

# 跟进状态（系统自动更新；follow_up 可人工标记）
WORKFLOW_STATUSES: dict[str, str] = {
    "new": "新客户",
    "quoting": "待报价",
    "quoted": "已报价",
    "escalated": "待人工",
    "human": "人工接管",
    "follow_up": "跟进中",
}

# 销售管道独立于机器人/人工跟进状态，避免把「人工接管」误当成成交阶段。
SALES_STAGES: dict[str, str] = {
    "new_lead": "新线索",
    "contacted": "已联系",
    "qualified": "需求确认",
    "design_discussion": "方案沟通",
    "quotation": "报价",
    "site_assessment": "现场评估",
    "contract": "合同",
    "closed": "已结束",
}

SALES_STAGE_NEXT_ACTIONS: dict[str, str | None] = {
    "new_lead": "首次联系客户",
    "contacted": "确认项目基本需求",
    "qualified": "讨论户型与设计方案",
    "design_discussion": "准备并发送报价",
    "quotation": "确认报价反馈并安排现场评估",
    "site_assessment": "确认评估结果并推进合同",
    "contract": "跟进合同签署",
    "closed": None,
}

# 状态标签（筛选 / 客服标记；部分可系统自动判断）
CUSTOMER_STATUSES: dict[str, str] = {
    "new": "新客户",
    "no_reply": "未回复",
    "needs_follow_up": "需跟进",
    "vip": "重点客户",
    "partner": "合作伙伴",
}

# 兼容旧引用
CONTACT_CATEGORIES = CONTACT_TYPES

# 港口大城市：显示名用中文；80km 用邮编环 + 常见郊区近似（无 GPS 时）
MAJOR_CITIES: list[dict[str, Any]] = [
    {
        "id": "sydney",
        "label_zh": "悉尼",
        "state": "NSW",
        "aliases": ("sydney", "悉尼", "雪梨"),
        "suburbs": (
            "parramatta", "penrith", "liverpool", "blacktown", "chatswood",
            "bondi", "manly", "cronulla", "hurstville", "bankstown", "ryde",
            "hornsby", "campbelltown", "sutherland", "strathfield",
        ),
        "postcode_ranges": ((2000, 2249), (2555, 2574), (2745, 2786)),
    },
    {
        "id": "melbourne",
        "label_zh": "墨尔本",
        "state": "VIC",
        "aliases": ("melbourne", "墨尔本", "维州墨尔本"),
        "suburbs": (
            "geelong", "werribee", "dandenong", "frankston", "box hill",
            "ringwood", "sunshine", "footscray", "st kilda", "south yarra",
            "preston", "brunswick", "richmond", "carlton", "docklands",
        ),
        "postcode_ranges": (
            (3000, 3211), (3335, 3341), (3427, 3442),
            (3750, 3811), (3910, 3920), (3926, 3944), (3975, 3978), (3980, 3983),
        ),
    },
    {
        "id": "brisbane",
        "label_zh": "布里斯班",
        "state": "QLD",
        "aliases": ("brisbane", "布里斯班", "布里斯本"),
        "suburbs": (
            "ipswich", "logan", "redcliffe", "caboolture", "chermside",
            "south bank", "fortitude valley", "toowong", "indooroopilly",
        ),
        "postcode_ranges": ((4000, 4207), (4300, 4306), (4500, 4510)),
    },
    {
        "id": "gold_coast",
        "label_zh": "黄金海岸",
        "state": "QLD",
        "aliases": ("gold coast", "黄金海岸", "goldcoast"),
        "suburbs": (
            "surfers paradise", "broadbeach", "southport", "burleigh",
            "coolangatta", "robina", "nerang",
        ),
        "postcode_ranges": ((4208, 4229), (2485, 2486)),
    },
    {
        "id": "sunshine_coast",
        "label_zh": "阳光海岸",
        "state": "QLD",
        "aliases": ("sunshine coast", "阳光海岸", "sunshinecoast"),
        "suburbs": (
            "maroochydore", "caloundra", "noosa", "nambour", "mooloolaba",
        ),
        "postcode_ranges": ((4550, 4575),),
    },
    {
        "id": "adelaide",
        "label_zh": "阿德莱德",
        "state": "SA",
        "aliases": ("adelaide", "阿德莱德"),
        "suburbs": (
            "glenelg", "port adelaide", "norwood", "unley", "prospect",
            "mawson lakes", "modbury", "marion", "salisbury",
        ),
        "postcode_ranges": ((5000, 5199),),
    },
    {
        "id": "newcastle",
        "label_zh": "纽卡斯尔",
        "state": "NSW",
        "aliases": ("newcastle", "纽卡斯尔"),
        "suburbs": ("maitland", "lake macquarie", "charlestown", "wallsend"),
        "postcode_ranges": ((2280, 2338),),
    },
    {
        "id": "wollongong",
        "label_zh": "卧龙岗",
        "state": "NSW",
        "aliases": ("wollongong", "卧龙岗", "illawarra"),
        "suburbs": ("shellharbour", "dapto", "corrimal", "fairy meadow"),
        "postcode_ranges": ((2500, 2530),),
    },
    {
        "id": "canberra",
        "label_zh": "堪培拉",
        "state": "ACT",
        "aliases": ("canberra", "堪培拉", "坎培拉"),
        "suburbs": ("belconnen", "woden", "tuggeranong", "gungahlin", "civic"),
        "postcode_ranges": ((2600, 2618), (2900, 2914)),
    },
    {
        "id": "perth",
        "label_zh": "珀斯",
        "state": "WA",
        "aliases": ("perth", "珀斯", "佩斯", "伯斯"),
        "suburbs": (
            "fremantle", "joondalup", "rockingham", "mandurah", "midland",
            "subiaco", "scarborough",
        ),
        "postcode_ranges": ((6000, 6199),),
    },
]

PORT_CITY_IDS = {
    "sydney",
    "melbourne",
    "brisbane",
    "gold_coast",
    "sunshine_coast",
    "perth",
    "adelaide",
    "newcastle",
    "wollongong",
}

STATE_LABEL_ZH: dict[str, str] = {
    "NSW": "新州",
    "VIC": "维州",
    "QLD": "昆州",
    "SA": "南澳",
    "WA": "西澳",
    "TAS": "塔州",
    "ACT": "首都领地",
    "NT": "北领地",
}


def contact_type_label(code: str) -> str:
    return CONTACT_TYPES.get(code, code or "未分类")


def workflow_status_label(code: str) -> str:
    return WORKFLOW_STATUSES.get(code, code or "新客户")


def customer_status_label(code: str) -> str:
    return CUSTOMER_STATUSES.get(code, code or "新客户")


def sales_stage_label(code: str) -> str:
    return SALES_STAGES.get(code, code or "新线索")


def infer_sales_stage(
    *,
    current: str,
    history: list[dict[str, Any]],
    requirements: RequirementState,
    last_quote: dict[str, Any] | None,
    sent_template_keys: list[str],
) -> str:
    """只向前自动推进到报价；现场评估、合同和结束必须人工确认。"""
    order = tuple(SALES_STAGES)
    current = current if current in SALES_STAGES else "new_lead"
    target = current

    def advance(candidate: str) -> None:
        nonlocal target
        if order.index(candidate) > order.index(target):
            target = candidate

    if any(item.get("role") in ("assistant", "agent") for item in history):
        advance("contacted")
    if requirements.state is not None and requirements.bedrooms is not None:
        advance("qualified")
    if requirements.approx_floor_area_m2 is not None or any(
        key.endswith("s") and key[:-1].isdigit() for key in sent_template_keys
    ):
        advance("design_discussion")
    if last_quote or any(key.endswith("price") for key in sent_template_keys):
        advance("quotation")
    return target


def category_label(code: str) -> str:
    return contact_type_label(code)


def extract_whatsapp_phone(session_id: str) -> str | None:
    """从 wa-+614... 会话 ID 解析完整 WhatsApp 号码。"""
    if session_id.startswith("wa-"):
        phone = session_id.removeprefix("wa-").strip()
        return phone or None
    return None


def phone_last4(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return None
    return digits[-4:]


def _postcode_in_ranges(postcode: str | None, ranges: tuple[tuple[int, int], ...]) -> bool:
    if not postcode or not postcode.isdigit():
        return False
    value = int(postcode)
    return any(lo <= value <= hi for lo, hi in ranges)


def match_major_city(
    *,
    suburb: str | None = None,
    postcode: str | None = None,
    text: str | None = None,
) -> dict[str, Any] | None:
    """匹配港口大城市或其约 80km 圈（邮编/郊区近似）。"""
    blob = " ".join(p for p in (suburb or "", text or "") if p).lower()
    for city in MAJOR_CITIES:
        for alias in city["aliases"]:
            if alias.lower() in blob:
                return city
        if suburb:
            sub = suburb.lower().strip()
            if any(s in sub or sub in s for s in city["suburbs"]):
                return city
        if _postcode_in_ranges(postcode, city["postcode_ranges"]):
            return city
    return None


def resolve_location_prefix(
    requirements: RequirementState,
    *,
    city: str | None = None,
    state: str | None = None,
    postcode: str | None = None,
    address: str | None = None,
) -> str | None:
    """有明确地址信息时返回前缀：大城市中文名或州简称。"""
    suburb = (requirements.suburb_or_lga or city or "").strip() or None
    pc = (requirements.postcode or postcode or "").strip() or None
    street = (requirements.street_address or address or "").strip() or None
    st = infer_state(requirements) or (state.strip().upper() if state else None)

    has_clear_address = bool(suburb or street or pc)
    if not has_clear_address:
        return None

    major = match_major_city(suburb=suburb, postcode=pc, text=f"{suburb or ''} {street or ''}")
    if major:
        return str(major["label_zh"])

    if st:
        return STATE_LABEL_ZH.get(st, st)
    return None


def resolve_personal_name(
    requirements: RequirementState,
    *,
    whatsapp_profile_name: str | None = None,
) -> str | None:
    """客户个人姓名（不含大城市/州前缀）。优先问卷姓名，其次 WhatsApp 资料名。"""
    personal = (requirements.contact_name or "").strip() or None
    if personal:
        return personal
    if whatsapp_profile_name:
        return whatsapp_profile_name.strip() or None
    return None


def location_prefix_labels() -> tuple[str, ...]:
    """显示名前缀候选（大城市中文名 + 州简称），长的优先。"""
    cities = sorted((str(c["label_zh"]) for c in MAJOR_CITIES), key=len, reverse=True)
    states = sorted(STATE_LABEL_ZH.values(), key=len, reverse=True)
    return tuple(cities + states)


def strip_location_prefix(display_name: str | None) -> str:
    """从显示名去掉大城市/州前缀，得到个人名或后四位。"""
    s = (display_name or "").strip()
    if not s:
        return ""
    for prefix in location_prefix_labels():
        if s.startswith(prefix) and len(s) > len(prefix):
            return s[len(prefix) :].strip()
    return s


def infer_display_name(
    requirements: RequirementState,
    *,
    session_id: str,
    manual_name: str | None = None,
    whatsapp_profile_name: str | None = None,
    name_manual: bool = False,
    city: str | None = None,
    state: str | None = None,
    postcode: str | None = None,
    address: str | None = None,
    whatsapp_phone: str | None = None,
) -> str:
    """通讯录显示名：姓名优先，否则完整电话（不再加城市/州前缀）。"""
    del city, state, postcode, address  # 保留参数兼容调用方；地址不参与显示名
    if name_manual and manual_name:
        return manual_name.strip()

    personal = resolve_personal_name(
        requirements, whatsapp_profile_name=whatsapp_profile_name
    )
    if personal:
        return personal

    phone = whatsapp_phone or extract_whatsapp_phone(session_id)
    if phone:
        return phone
    if session_id.startswith("web-"):
        return f"网页客户 {session_id[-6:]}"
    if session_id.startswith("cli-"):
        return f"CLI {session_id[-6:]}"
    return session_id[:20]


def infer_city(requirements: RequirementState) -> str | None:
    """城市栏：优先大城市中文名，否则 suburb。"""
    suburb = (requirements.suburb_or_lga or "").strip() or None
    pc = (requirements.postcode or "").strip() or None
    major = match_major_city(suburb=suburb, postcode=pc, text=suburb)
    if major:
        return str(major["label_zh"])
    return suburb


def infer_state(requirements: RequirementState) -> str | None:
    if requirements.state is None:
        return None
    return str(
        requirements.state.value
        if hasattr(requirements.state, "value")
        else requirements.state
    )


def infer_postcode(requirements: RequirementState) -> str | None:
    if not requirements.postcode:
        return None
    return str(requirements.postcode).strip()


def infer_address(requirements: RequirementState) -> str | None:
    """根据对话抽取的街道/郊区/州/邮编自动拼客户地址（州单独出现不算地址）。"""
    street = (requirements.street_address or "").strip() or None
    suburb = (requirements.suburb_or_lga or "").strip() or None
    state = infer_state(requirements)
    postcode = (str(requirements.postcode).strip() if requirements.postcode else "") or None

    # 仅有州（引导问答只答了 NSW/VIC）时不写入地址栏
    if not street and not suburb and not postcode:
        return None

    # 澳大利亚常见写法：12 George St, Parramatta NSW 2150
    line_parts: list[str] = []
    if street:
        line_parts.append(street)

    locality: list[str] = []
    if suburb:
        locality.append(suburb)
    if state and (not suburb or suburb.upper() != state):
        locality.append(state)
    if postcode:
        locality.append(postcode)
    if locality:
        line_parts.append(" ".join(locality))

    text = ", ".join(line_parts).strip(" ,")
    return text or None


def normalize_address(address: str | None, postcode: str | None = None) -> str:
    """Collapse malformed/repeated address fragments and keep the postcode inline."""
    fragments = [part.strip() for part in re.split(r"\s*,+\s*", address or "") if part.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for fragment in fragments:
        key = " ".join(fragment.casefold().split())
        if key not in seen:
            seen.add(key)
            unique.append(" ".join(fragment.split()))
    text = ", ".join(unique)
    code = (postcode or "").strip()
    if code and not re.search(rf"\b{re.escape(code)}\b", text):
        text = f"{text}{' ' if text else ''}{code}"
    return text


def format_location_summary(
    *,
    state: str | None,
    city: str | None,
    postcode: str | None,
    address: str | None,
) -> str:
    """侧栏摘要：避免州/城市与完整地址重复堆叠。"""
    if address:
        return normalize_address(address, postcode)
    parts = [p for p in (city, state, postcode) if p]
    # 去重（如 city 已是州码）
    seen: set[str] = set()
    ordered: list[str] = []
    for p in parts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return " · ".join(ordered)


def infer_workflow_status(
    *,
    human_mode: bool,
    escalated: bool,
    has_quote: bool,
    questionnaire: dict[str, Any],
    current: str = "new",
) -> str:
    if current == "follow_up":
        return current
    if human_mode:
        return "human"
    if escalated:
        return "escalated"
    if has_quote:
        return "quoted"
    if questionnaire.get("state") or questionnaire.get("bedrooms"):
        return "quoting"
    return "new"


def infer_customer_status(
    *,
    history: list[dict[str, Any]],
    escalated: bool,
    workflow_status: str,
    human_mode: bool,
    current: str = "new",
    manual: bool = False,
) -> str:
    """状态标签：客服手动标记优先，否则按聊天记录自动推断。"""
    if manual and current in CUSTOMER_STATUSES:
        return current

    if not history:
        return "new"

    last_role = str(history[-1].get("role", ""))
    user_messages = sum(1 for m in history if m.get("role") == "user")
    staff_messages = sum(
        1 for m in history if m.get("role") in ("assistant", "agent")
    )

    if user_messages == 1 and staff_messages == 0:
        return "new"

    if last_role == "user" and (human_mode or staff_messages > 0):
        return "no_reply"

    if escalated or workflow_status in ("follow_up", "quoted", "escalated"):
        return "needs_follow_up"

    if user_messages <= 1 and staff_messages <= 1:
        return "new"

    return "needs_follow_up" if staff_messages > 0 else "new"


def infer_contact_type(text: str) -> str | None:
    """从客户消息粗判角色分类（LLM 未识别时的补充）。"""
    lower = text.lower()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("builder", ("builder", "construction", "contractor", "建造", "施工", "建筑商")),
        ("investor", ("investor", "investment buyer", "投资客")),
        ("distributor", ("distributor", "dealer", "reseller", "经销商", "分销商")),
        ("agent", ("real estate", "realtor", "estate agent", "代理", "中介", "经纪")),
        ("consultant", ("consultant", "advisor", "adviser", "顾问", "咨询")),
        ("designer", ("designer", "architect", "draftsperson", "设计师", "设计", "建筑师")),
        ("owner", ("owner-occupier", "owner occupier", "homeowner", "land owner", "property owner", "业主", "房东")),
        ("other", ("others", "other role", "其他")),
    ]
    for code, keywords in rules:
        if any(k in lower for k in keywords):
            return code
    return None


def extract_unit_count_from_text(text: str) -> int | None:
    """Extract an explicitly stated number of planned dwellings/units."""
    if not text:
        return None
    lower = text.lower()
    match = re.search(
        r"\b(\d{1,2})\s*(?:x\s*)?(?:units?|homes?|houses?|dwellings?|granny\s*flats?)\b",
        lower,
    )
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 100 else None
    word_numbers = {"two": 2, "three": 3, "four": 4, "five": 5}
    for word, value in word_numbers.items():
        if re.search(
            rf"\b{word}\s+(?:units?|homes?|houses?|dwellings?|granny\s*flats?)\b",
            lower,
        ):
            return value
    cn_match = re.search(r"(?:购买|建|要|计划)?\s*([一二两三四五六七八九十]|\d{1,2})\s*套", text)
    if cn_match:
        raw = cn_match.group(1)
        cn_numbers = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        value = cn_numbers.get(raw, int(raw) if raw.isdigit() else 0)
        return value if 1 <= value <= 100 else None
    return None


_AU_POSTCODE_RE = re.compile(r"\b(\d{4})\b")
_STREET_RE = re.compile(
    r"\b(\d+[A-Za-z]?\s+[A-Za-z][A-Za-z\s]{1,40}?"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|Way|Parade|Pde|Lane|Ln))\b",
    re.I,
)
_CN_ADDR_RE = re.compile(
    r"(?:地址[是为:：\s]*|住在|在)"
    r"([^\n,，。]{4,80}(?:路|街|道|号|巷|弄))"
)


def extract_postcode_from_text(text: str) -> str | None:
    match = _AU_POSTCODE_RE.search(text)
    return match.group(1) if match else None


def extract_location_from_text(text: str) -> dict[str, Any]:
    """从客户原话抽取州/城市/街道/邮编（补充 LLM patch）。"""
    patch: dict[str, Any] = {}
    if not text:
        return patch
    lower = text.lower()

    for state in ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"):
        if re.search(rf"\b{state.lower()}\b", lower):
            patch["state"] = state
            break
    for zh, code in (
        ("新南威尔士", "NSW"),
        ("维州", "VIC"),
        ("维多利亚", "VIC"),
        ("昆士兰", "QLD"),
        ("南澳", "SA"),
        ("西澳", "WA"),
        ("塔斯马尼亚", "TAS"),
        ("北领地", "NT"),
    ):
        if zh in text:
            patch.setdefault("state", code)

    major = match_major_city(text=text)
    if major:
        patch.setdefault("state", major["state"])
        patch.setdefault("suburb_or_lga", major["label_zh"])

    suburb_m = re.search(
        r"\bin\s+([A-Za-z][A-Za-z\s]{1,40}?)(?:\s*,?\s*(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT|\d{4})\b|$)",
        text,
        re.I,
    )
    if suburb_m and "melbourne" not in suburb_m.group(1).lower():
        cand = suburb_m.group(1).strip(" ,.")
        if len(cand) >= 3 and cand.lower() not in ("the", "our", "my"):
            patch.setdefault("suburb_or_lga", cand)

    street = _STREET_RE.search(text)
    if street:
        patch["street_address"] = street.group(1).strip()
    cn_addr = _CN_ADDR_RE.search(text)
    if cn_addr:
        patch.setdefault("street_address", cn_addr.group(1).strip())

    postcode = extract_postcode_from_text(text)
    if postcode:
        patch["postcode"] = postcode
        city = match_major_city(postcode=postcode)
        if city:
            patch.setdefault("state", city["state"])
            if "suburb_or_lga" not in patch:
                patch["suburb_or_lga"] = city["label_zh"]

    return patch


def sync_address_from_requirements(session: Any) -> None:
    """聊天抽取到的地址字段同步到通讯录（地址栏由对话自动生成）。"""
    req = session.requirements
    state = infer_state(req)
    city = infer_city(req)
    postcode = infer_postcode(req)
    address = infer_address(req)

    if state:
        session.state = state
    if city:
        session.city = city
    if postcode:
        session.postcode = postcode
    # 有可拼地址则覆盖；若问卷已无实质地址信息则清空旧的「仅州」脏数据
    if address:
        session.address = address
    elif session.address and session.address.strip().upper() in {
        "NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT",
    }:
        session.address = None

    if session.contact_type == "unknown":
        req_type = getattr(req, "contact_type", None)
        if req_type and req_type in CONTACT_TYPES:
            session.contact_type = req_type
