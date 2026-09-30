from grannyflat_bot.session.contacts import format_location_summary, normalize_address


def test_normalize_address_removes_duplicate_fragments_and_bad_commas() -> None:
    value = "布里斯班 QLD 4169,, 布里斯班 QLD 4169"
    assert normalize_address(value, "4169") == "布里斯班 QLD 4169"
    assert format_location_summary(
        state="QLD", city="布里斯班", postcode="4169", address=value
    ) == "布里斯班 QLD 4169"


def test_normalize_address_keeps_distinct_street_and_locality() -> None:
    assert normalize_address("12 George St, Parramatta NSW", "2150") == (
        "12 George St, Parramatta NSW 2150"
    )
