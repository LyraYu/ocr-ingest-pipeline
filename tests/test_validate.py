import pytest

from app.extraction.validate import (
    parse_amount,
    parse_date,
    parse_datetime,
    validate_field,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("08/03/2026", "08/03/2026"),
        ("8/3/2026", "08/03/2026"),
        ("08-03-2026", "08/03/2026"),
        ("08.03.2026", "08/03/2026"),
        ("2026-03-08", "08/03/2026"),
        ("8 Mar 2026", "08/03/2026"),
        ("8 March 2026", "08/03/2026"),
        ("08-Sept-2026", "08/09/2026"),
        ("Mar 8, 2026", "08/03/2026"),
        (" 14/03/1988 ", "14/03/1988"),
    ],
)
def test_date_forms(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "08/03/26", "13/13/2026", "yesterday", ""])
def test_invalid_dates(raw):
    with pytest.raises(ValueError):
        parse_date(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02/09/2026 10:20", "02/09/2026 10:20"),
        ("2/9/2026 9:05", "02/09/2026 09:05"),
        ("2026-09-02T10:20:33", "02/09/2026 10:20"),
        ("2 Sep 2026, 3:15 pm", "02/09/2026 15:15"),
        ("02/09/2026 12:05 AM", "02/09/2026 00:05"),
    ],
)
def test_datetime_forms(raw, expected):
    assert parse_datetime(raw) == expected


@pytest.mark.parametrize("raw", ["02/09/2026", "02/09/2026 25:00", "02/09/2026 13:00 pm"])
def test_invalid_datetimes(raw):
    with pytest.raises(ValueError):
        parse_datetime(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("S$93.20", 9320),
        ("SGD 1,200.00", 120000),
        ("S$1,234.50", 123450),
        ("$8.5", 850),
        ("S$45", 4500),
        ("850.00 SGD", 85000),
        ("RM 12.30", 1230),
        ("VND 1,200,000", 1200000),
        ("7.70", 770),
    ],
)
def test_amounts_in_smallest_unit(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["-S$5.00", "S$-5.00", "EUR 5.00", "S$1.234", "1,20", "abc"])
def test_invalid_amounts(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_validate_fields():
    # dates → DD/MM/YYYY
    assert validate_field("date_of_mc", "date", "8 Mar 2026").normalised_value == "08/03/2026"
    assert validate_field("claimant_date_of_birth", "date", "1988-03-14").normalised_value == "14/03/1988"
    # amounts: currency, separators and the decimal point removed
    assert validate_field("total_amount", "amount", "S$93.20").normalised_value == 9320
    assert validate_field("total_requested_amount", "amount", "SGD 1,200.00").normalised_value == 120000
    # provider_name must not name Fullerton Health
    bad = validate_field("provider_name", "text", "Panel clinic of Fullerton Health network")
    assert bad.status == "invalid" and bad.normalised_value is None and "Fullerton Health" in bad.message
    assert validate_field("provider_name", "text", "FULLERTON  HEALTH Clinic").status == "invalid"
    assert validate_field("provider_name", "text", "Harbourview Family Clinic").normalised_value == (
        "Harbourview Family Clinic"
    )
    # mc_days: non-negative whole number
    assert validate_field("mc_days", "int", "-1").status == "invalid"
    assert validate_field("mc_days", "int", "2.5").status == "invalid"
    ok = validate_field("mc_days", "int", "2")
    assert ok.status == "valid" and ok.normalised_value == 2
    # missing
    missing = validate_field("icd_code", "text", None)
    assert missing.status == "missing" and missing.normalised_value is None
    # signature heuristic
    assert validate_field("signature_presence", "bool", "[signed]").normalised_value is True


def test_invalid_messages_do_not_echo_the_value():
    result = validate_field("claimant_date_of_birth", "date", "S1234567A")
    assert result.status == "invalid" and "S1234567A" not in result.message
