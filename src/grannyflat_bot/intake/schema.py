"""报价问卷结构化字段。

字段名保持英文（对接 LLM / WhatsApp / 规则引擎）；注释用中文方便国内同事维护。
对客展示文案在 orchestrator / quote 模块中用英文生成。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AustralianState(str, Enum):
    """澳洲州/领地代码。"""

    NSW = "NSW"
    VIC = "VIC"
    QLD = "QLD"
    SA = "SA"
    WA = "WA"
    TAS = "TAS"
    ACT = "ACT"
    NT = "NT"


class DeliveryType(str, Enum):
    """交付方式：kit=只供模块；managed_build=协调建造；turnkey=交钥匙。"""

    kit = "kit"
    managed_build = "managed_build"
    turnkey = "turnkey"


class RequirementState(BaseModel):
    """会话中积累的客户需求问卷（用于示意报价）。"""

    state: AustralianState | None = None  # 项目所在州
    suburb_or_lga: str | None = None  # suburb 或地方政府区
    lot_size_m2: float | None = None  # 地块面积 m²
    approx_floor_area_m2: float | None = None  # 目标建筑面积 m²
    bedrooms: int | None = Field(default=None, ge=0)  # Requested bedrooms; not a claim of catalog availability. 0=studio
    bathrooms: float | None = Field(default=None, ge=0, le=5)
    delivery_type: DeliveryType | None = None
    include_council: bool | None = None  # 是否含许可协助
    include_design: bool | None = None  # 是否含设计文件
    include_foundations: bool | None = None  # 是否含基础
    include_services: bool | None = None  # 是否含水电等接驳包
    timeline: str | None = None  # 工期偏好（自由文本）
    budget_aud_min: float | None = None
    budget_aud_max: float | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    postcode: str | None = None  # 澳洲邮编
    street_address: str | None = None  # 街道地址
    contact_type: str | None = None  # owner | builder | agent | consultant | designer | investor | distributor | other
    usage_type: str | None = None  # 用途: owner_occupier 自住 | rental 出租 | investment 投资 | builder
    unit_count: int | None = Field(default=None, ge=1, le=100)  # 计划购买套数
    notes: str | None = None  # 备注 / 转人工摘要

    # 字段来源与置信度（审计 / 侧边栏）
    field_sources: dict[str, str] = Field(default_factory=dict)
    field_confidence: dict[str, float] = Field(default_factory=dict)

    @field_validator("state", mode="before")
    @classmethod
    def normalize_state(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator("delivery_type", mode="before")
    @classmethod
    def normalize_delivery(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "_").replace("-", "_")
            aliases = {
                "managed": "managed_build",
                "project_managed": "managed_build",
                "full": "turnkey",
            }
            return aliases.get(key, key)
        return v

    @field_validator("contact_type", mode="before")
    @classmethod
    def normalize_contact_type(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "_").replace("-", "_")
            aliases = {
                "homeowner": "owner",
                "landowner": "owner",
                "property_owner": "owner",
                "construction": "builder",
                "contractor": "builder",
                "realtor": "agent",
                "broker": "agent",
                "real_estate": "agent",
                "advisor": "consultant",
                "adviser": "consultant",
                "architect": "designer",
                "owner_occupier": "owner",
                "investment_buyer": "investor",
                "dealer": "distributor",
                "reseller": "distributor",
                "others": "other",
            }
            return aliases.get(key, key)
        return v

    def missing_fields(self, required: list[str]) -> list[str]:
        """返回仍为空的必填字段名列表。"""
        data = self.model_dump()
        missing = []
        for name in required:
            if data.get(name) is None:
                missing.append(name)
        return missing

    def apply_patch(
        self,
        patch: dict[str, Any],
        *,
        source: str = "llm",
        confidence: float = 0.8,
    ) -> RequirementState:
        """合并非空补丁；记录来源与置信度。返回新状态（不可变风格）。"""
        allowed = set(type(self).model_fields) - {"field_sources", "field_confidence"}
        updates: dict[str, Any] = {}
        sources = dict(self.field_sources)
        conf = dict(self.field_confidence)
        for key, value in patch.items():
            if key not in allowed or value is None or value == "":
                continue
            updates[key] = value
            sources[key] = source
            conf[key] = confidence
        if not updates:
            return self
        # Re-validate so enum/coercion validators run (model_copy update skips them).
        payload = self.model_dump()
        payload.update(updates)
        payload["field_sources"] = sources
        payload["field_confidence"] = conf
        return RequirementState.model_validate(payload)

    def as_questionnaire(self) -> dict[str, Any]:
        """侧边栏 / WhatsApp 摘要用的精简视图（去掉审计字段）。"""
        data = self.model_dump(
            mode="json",
            exclude={"field_sources", "field_confidence"},
            exclude_none=True,
        )
        return data


# 示意报价最低必填：州、卧室数、交付方式
REQUIRED_FOR_QUOTE = ["state", "bedrooms", "delivery_type"]
