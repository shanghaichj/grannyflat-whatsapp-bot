from grannyflat_bot.intake.schema import RequirementState
from grannyflat_bot.quote.engine import QuoteEngine


def test_quote_requires_fields():
    engine = QuoteEngine()
    result = engine.quote(RequirementState(state="NSW"))
    assert result.can_quote is False
    assert "bedrooms" in result.missing_required
    assert "delivery_type" in result.missing_required


def test_quote_deterministic():
    engine = QuoteEngine()
    req = RequirementState(
        state="VIC",
        bedrooms=2,
        bathrooms=1,
        delivery_type="turnkey",
        approx_floor_area_m2=68,
        include_council=True,
        include_design=True,
        include_foundations=False,
        include_services=True,
    )
    a = engine.quote(req)
    b = engine.quote(req)
    assert a.can_quote is True
    assert a.subtotal == b.subtotal
    assert a.range_low == b.range_low
    assert a.range_high == b.range_high
    assert a.subtotal > 0
    assert a.range_low < a.subtotal < a.range_high


def test_format_message_bilingual():
    engine = QuoteEngine()
    req = RequirementState(state="VIC", bedrooms=2, delivery_type="kit")
    result = engine.quote(req)
    msg = result.format_message(bilingual=True)
    assert "示意报价" in msg
    assert "Indicative quote" in msg

    engine = QuoteEngine()
    base = dict(
        bedrooms=1,
        delivery_type="kit",
        approx_floor_area_m2=50,
    )
    vic = engine.quote(RequirementState(state="VIC", **base))
    wa = engine.quote(RequirementState(state="WA", **base))
    assert wa.subtotal > vic.subtotal
