"""户型商品包：客服输入「发送60」等指令时匹配。

Content ID 来自 Meta Commerce Manager「内容编号」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogItem:
    name: str
    content_id: str  # Meta 内容编号 / Product Retailer ID


@dataclass(frozen=True)
class CatalogPack:
    key: str  # e.g. "60"
    title: str
    body: str
    subtitle: str
    section_title: str
    items: tuple[CatalogItem, ...]

    @property
    def thumbnail_id(self) -> str:
        return self.items[0].content_id if self.items else ""


# 60㎡ 系列：A60 / B60 / C60 / D60（截图中的内容编号）
PACK_60 = CatalogPack(
    key="60",
    title="60m2 Series",
    body="Here are our 60m2 granny flat floor plans. Tap to view details.",
    subtitle="A60 B60 C60 D60",
    section_title="60m2",
    items=(
        CatalogItem("A60", "cwvx44xslm"),
        CatalogItem("B60", "czdxf1wwzu"),
        CatalogItem("C60", "tbo8j4glc2"),
        CatalogItem("D60", "3i1gbfvwkm"),
    ),
)

CATALOG_PACKS: dict[str, CatalogPack] = {
    PACK_60.key: PACK_60,
}

# 发送60 / 发送 60 / send60 / send 60m2 / 发送60平方
_COMMAND_RE = re.compile(
    r"^\s*(?:发送|send)\s*(\d+)\s*(?:m2|㎡|平米|平方米|平方)?\s*$",
    re.IGNORECASE,
)


def match_catalog_command(text: str) -> CatalogPack | None:
    """识别客服聊天框中的目录发送指令。"""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _COMMAND_RE.match(raw)
    if not m:
        return None
    return CATALOG_PACKS.get(m.group(1))


def pack_to_twilio_products(pack: CatalogPack) -> list[dict[str, str]]:
    """Twilio ContentVariables.products 所需结构。"""
    return [
        {"id": item.content_id, "section_title": pack.section_title}
        for item in pack.items
    ]
