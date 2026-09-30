"""规则引擎报价：算术在 YAML 规则里完成，不由大模型算价。

对客展示文案用英文；本文件注释用中文。切勿把工厂成本底表原样写进客户可见文案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grannyflat_bot.config import PRICING_PATH
from grannyflat_bot.intake.schema import RequirementState


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


@dataclass
class LineItem:
    """报价分项（内部 code；label 对客英文）。"""

    code: str
    label: str
    amount_aud: float


@dataclass
class QuoteResult:
    """示意报价结果：区间 + 免责声明；can_quote=False 时需继续收集字段。"""

    currency: str
    line_items: list[LineItem]
    subtotal: float
    range_low: float
    range_high: float
    assumptions: list[str] = field(default_factory=list)
    missing_for_firm_quote: list[str] = field(default_factory=list)
    disclaimer: str = ""
    can_quote: bool = True
    missing_required: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "can_quote": self.can_quote,
            "missing_required": self.missing_required,
            "line_items": [
                {"code": i.code, "label": i.label, "amount_aud": i.amount_aud}
                for i in self.line_items
            ],
            "subtotal": self.subtotal,
            "range_low": self.range_low,
            "range_high": self.range_high,
            "assumptions": self.assumptions,
            "missing_for_firm_quote": self.missing_for_firm_quote,
            "disclaimer": self.disclaimer,
        }

    def format_message(self, *, bilingual: bool = False) -> str:
        """生成报价摘要；Web 可中英双语，WhatsApp 仅英文。"""
        if not self.can_quote:
            missing = ", ".join(self.missing_required)
            if bilingual:
                return (
                    f"报价前还需要以下信息：{missing}。\n\n"
                    f"---\n\n"
                    f"I still need a few details before an estimate: {missing}. "
                    "I'll review the model, delivery scope and site once we have them."
                )
            return (
                f"I still need a few details before an estimate: {missing}. "
                "I'll review the model, delivery scope and site once we have them."
            )
        if bilingual:
            lines = [
                f"*示意报价 Indicative quote* ({self.currency})",
                "",
            ]
        else:
            lines = [
                f"*Indicative quote* ({self.currency})",
                "",
            ]
        for item in self.line_items:
            lines.append(f"• {item.label}: ${item.amount_aud:,.0f}")
        if bilingual:
            lines.extend(
                [
                    "",
                    f"中间价 Subtotal (mid): ${self.subtotal:,.0f}",
                    f"示意区间 Indicative range: ${self.range_low:,.0f} – ${self.range_high:,.0f}",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    f"Subtotal (mid): ${self.subtotal:,.0f}",
                    f"Indicative range: ${self.range_low:,.0f} – ${self.range_high:,.0f}",
                ]
            )
        if self.assumptions:
            lines.append("")
            lines.append("假设 Assumptions:" if bilingual else "Assumptions:")
            for a in self.assumptions:
                lines.append(f"• {a}")
        if self.missing_for_firm_quote:
            lines.append("")
            if bilingual:
                lines.append(
                    "更精确报价仍建议补充 / For a firmer range, still useful: "
                    + ", ".join(self.missing_for_firm_quote)
                )
            else:
                lines.append(
                    "For a firmer range, still useful: "
                    + ", ".join(self.missing_for_firm_quote)
                )
        if self.disclaimer:
            lines.extend(["", self.disclaimer])
        return "\n".join(lines)


class QuoteEngine:
    """Deterministic rule-based pricing. LLM must not invent numbers."""

    def __init__(self, rules_path: Path | None = None) -> None:
        self.rules_path = rules_path or PRICING_PATH
        self.rules = self._load_rules(self.rules_path)

    @staticmethod
    def _load_rules(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Pricing rules not found: {path}")
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("Pricing rules must be a YAML mapping")
        return data

    def reload(self) -> None:
        self.rules = self._load_rules(self.rules_path)

    def quote(self, requirements: RequirementState) -> QuoteResult:
        required = list(self.rules.get("required_for_quote") or ["state", "bedrooms", "delivery_type"])
        firm_required = list(
            self.rules.get("required_for_firm_range")
            or required
        )
        missing = requirements.missing_fields(required)
        currency = self.rules.get("currency", "AUD")
        disclaimer = self.rules.get("disclaimer", "")

        if missing:
            return QuoteResult(
                currency=currency,
                line_items=[],
                subtotal=0.0,
                range_low=0.0,
                range_high=0.0,
                assumptions=[],
                missing_for_firm_quote=requirements.missing_fields(firm_required),
                disclaimer=disclaimer,
                can_quote=False,
                missing_required=missing,
            )

        bedrooms = int(requirements.bedrooms)  # type: ignore[arg-type]
        state = _enum_value(requirements.state)
        delivery = _enum_value(requirements.delivery_type) or "kit"

        base_map = {int(k): float(v) for k, v in (self.rules.get("base_by_bedrooms") or {}).items()}
        if bedrooms not in base_map:
            return QuoteResult(
                currency=currency,
                line_items=[],
                subtotal=0.0,
                range_low=0.0,
                range_high=0.0,
                assumptions=[],
                missing_for_firm_quote=[],
                disclaimer=disclaimer,
                can_quote=False,
                missing_required=[f"unsupported bedrooms={bedrooms}"],
            )

        base = base_map[bedrooms]
        delivery_mult = float(
            (self.rules.get("delivery_multipliers") or {}).get(delivery, 1.0)
        )
        state_factor = float((self.rules.get("state_factors") or {}).get(state, 1.0))

        line_items = [
            LineItem("base_kit", f"Base package ({bedrooms}-bed band)", base),
            LineItem(
                "delivery",
                f"Delivery type adjustment ({delivery} ×{delivery_mult})",
                round(base * (delivery_mult - 1.0), 2),
            ),
        ]

        after_delivery = base * delivery_mult
        state_adj = after_delivery * (state_factor - 1.0)
        line_items.append(
            LineItem(
                "state",
                f"State factor ({state} ×{state_factor})",
                round(state_adj, 2),
            )
        )
        running = after_delivery * state_factor

        assumptions: list[str] = [
            f"Bedroom band: {bedrooms} (0 = studio)",
            f"Delivery: {delivery}",
            f"State: {state}",
            "PLACEHOLDER rates — replace data/pricing/rules.yaml before production.",
        ]

        # Area adjustment
        area_ref_map = {
            int(k): float(v) for k, v in (self.rules.get("area_reference_m2") or {}).items()
        }
        area_rate = float(self.rules.get("area_rate_per_m2") or 0)
        if requirements.approx_floor_area_m2 is not None and bedrooms in area_ref_map:
            delta_m2 = float(requirements.approx_floor_area_m2) - area_ref_map[bedrooms]
            area_adj = delta_m2 * area_rate
            line_items.append(
                LineItem(
                    "area",
                    f"Floor area adjustment ({delta_m2:+.0f} m² × ${area_rate:,.0f})",
                    round(area_adj, 2),
                )
            )
            running += area_adj
            assumptions.append(
                f"Floor area {requirements.approx_floor_area_m2:.0f} m² vs ref {area_ref_map[bedrooms]:.0f} m²"
            )
        else:
            assumptions.append("Floor area not provided — using bedroom-band base only")

        options = self.rules.get("options") or {}
        option_flags = [
            ("include_council", "Council package"),
            ("include_design", "Design package"),
            ("include_foundations", "Foundations package"),
            ("include_services", "Services / connections package"),
        ]
        for field_name, label in option_flags:
            flag = getattr(requirements, field_name)
            amount = float(options.get(field_name) or 0)
            if flag is True and amount:
                line_items.append(LineItem(field_name, label, amount))
                running += amount
            elif flag is None:
                assumptions.append(f"{label}: not specified (excluded from mid price)")

        missing_firm = requirements.missing_fields(firm_required)
        complete = len(missing_firm) == 0
        frac = float(
            self.rules.get("range_fraction_complete" if complete else "range_fraction_partial")
            or 0.1
        )
        subtotal = round(running, 2)
        range_low = round(subtotal * (1 - frac), 2)
        range_high = round(subtotal * (1 + frac), 2)

        return QuoteResult(
            currency=currency,
            line_items=line_items,
            subtotal=subtotal,
            range_low=range_low,
            range_high=range_high,
            assumptions=assumptions,
            missing_for_firm_quote=missing_firm,
            disclaimer=disclaimer,
            can_quote=True,
            missing_required=[],
        )
