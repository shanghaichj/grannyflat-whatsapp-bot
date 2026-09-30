from grannyflat_bot.intake.schema import RequirementState


def test_apply_patch_and_questionnaire():
    req = RequirementState()
    updated = req.apply_patch(
        {
            "state": "nsw",
            "bedrooms": 2,
            "delivery_type": "Turnkey",
            "include_council": True,
        },
        source="test",
    )
    assert updated.state.value == "NSW"
    assert updated.delivery_type.value == "turnkey"
    assert updated.field_sources["state"] == "test"
    q = updated.as_questionnaire()
    assert q["state"] == "NSW"
    assert q["bedrooms"] == 2


def test_missing_fields():
    req = RequirementState(state="QLD", bedrooms=1)
    assert req.missing_fields(["state", "bedrooms", "delivery_type"]) == [
        "delivery_type"
    ]
