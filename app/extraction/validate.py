"""Value validation and normalisation (docs/DESIGN.md §6, §6.1). Pure functions.

Each parser takes the raw OCR string and returns the normalised value or raises
ValueError. Messages never echo the raw value (it may be PII; raw_value is
stored separately).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

# --- dates -------------------------------------------------------------------

_NUMERIC_DMY = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_NAMED_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")


def _calendar_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError("not a calendar date") from None


def _parse_date(raw: str) -> date:
    s = raw.strip()
    if m := _NUMERIC_DMY.fullmatch(s):
        day, month, year = m.groups()
        if len(year) == 2:
            raise ValueError("two-digit year is ambiguous")
        # Day-first, as written in SG / MY / VN documents.
        return _calendar_date(int(year), int(month), int(day))
    if m := _ISO.fullmatch(s):
        year, month, day = map(int, m.groups())
        return _calendar_date(year, month, day)
    named = re.sub(r"\s+", " ", re.sub(r"[.,\-]", " ", s)).strip()
    named = re.sub(r"(?i)\bsept\b", "Sep", named)
    for fmt in _NAMED_FORMATS:
        try:
            return datetime.strptime(named, fmt).date()
        except ValueError:
            continue
    raise ValueError("unrecognised date format")


def parse_date(raw: str) -> str:
    """→ "DD/MM/YYYY". Accepts D/M/YYYY with / . - separators, YYYY-MM-DD,
    "8 Mar 2026", "8 March 2026", "Mar 8, 2026"."""
    return _parse_date(raw).strftime("%d/%m/%Y")


_DATETIME = re.compile(
    r"(?P<date>.+?)[ \t]*(?:T|,|at)?[ \t]*"
    r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?[ \t]*(?P<ampm>[ap]\.?m\.?)?",
    re.IGNORECASE,
)


def parse_datetime(raw: str) -> str:
    """→ "DD/MM/YYYY HH:MM" (24h). Date part as parse_date; time HH:MM[:SS] [am|pm]."""
    m = _DATETIME.fullmatch(raw.strip())
    if not m:
        raise ValueError("expected a date followed by a time (HH:MM)")
    day = _parse_date(m["date"])
    hour, minute = int(m["h"]), int(m["m"])
    if m["ampm"]:
        if not 1 <= hour <= 12:
            raise ValueError("12-hour time out of range")
        hour = hour % 12 + (12 if m["ampm"].lower().startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise ValueError("time out of range")
    return f"{day.strftime('%d/%m/%Y')} {hour:02d}:{minute:02d}"


# --- amounts -----------------------------------------------------------------

# Currency → number of minor-unit digits. Amounts are stored in the minor unit.
CURRENCY_EXPONENT = {
    "": 2, "$": 2, "S$": 2, "SGD": 2, "US$": 2, "USD": 2,
    "RM": 2, "MYR": 2, "PHP": 2, "₱": 2, "VND": 0, "₫": 0,
}
_AMOUNT = re.compile(
    r"(?P<pre>[^\d\-]*?)[ \t]*(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?[ \t]*(?P<post>\D*?)"
)


def parse_amount(raw: str) -> int:
    """→ integer in the currency's smallest unit: currency symbols, thousands
    separators and the decimal point removed (S$93.20 → 9320, SGD 1,200.00 →
    120000). An amount without decimals is scaled the same way (S$45 → 4500)."""
    m = _AMOUNT.fullmatch(raw.strip())
    if not m:
        raise ValueError("not an amount (negative or malformed number)")
    currencies = {c for c in (m["pre"].strip().upper(), m["post"].strip().upper()) if c}
    if len(currencies) > 1 or not currencies <= CURRENCY_EXPONENT.keys():
        raise ValueError("unknown currency")
    exponent = CURRENCY_EXPONENT[currencies.pop() if currencies else ""]
    decimals = m["dec"] or ""
    if len(decimals) > exponent:
        raise ValueError(f"more than {exponent} decimal places")
    return int(m["int"].replace(",", "") + decimals.ljust(exponent, "0"))


# --- ints, bools, text ---------------------------------------------------------


def parse_non_negative_int(raw: str) -> int:
    s = raw.strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    if re.fullmatch(r"-\d+(?:\.\d+)?", s):
        raise ValueError("must be non-negative")
    if re.fullmatch(r"\d+\.\d+", s):
        raise ValueError("must be a whole number")
    raise ValueError("not an integer")


_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def parse_bool(raw: str) -> bool:
    s = raw.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError("not a boolean")


def parse_signature_presence(raw: str) -> bool:
    """§6.1 heuristic: the rule matched "Signature" or "[signed]" → present."""
    if not raw.strip():
        raise ValueError("empty")
    return True


def parse_text(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw).strip()
    if not s:
        raise ValueError("empty")
    return s


def parse_provider_name(raw: str) -> str:
    """Text; invalid if it names Fullerton Health (the network, not the provider)."""
    s = parse_text(raw)
    if "fullerton health" in s.casefold():
        raise ValueError('provider_name must not contain "Fullerton Health"')
    return s


PARSERS_BY_TYPE = {
    "date": parse_date,
    "datetime": parse_datetime,
    "amount": parse_amount,
    "int": parse_non_negative_int,
    "bool": parse_bool,
    "text": parse_text,
}
PARSERS_BY_FIELD = {
    "provider_name": parse_provider_name,
    "signature_presence": parse_signature_presence,
}


@dataclass(frozen=True)
class Validation:
    status: str  # valid / invalid / missing
    normalised_value: object
    message: str | None


def validate_field(field_name: str, value_type: str, raw_value: str | None) -> Validation:
    if raw_value is None:
        return Validation("missing", None, "not found in document text")
    parser = PARSERS_BY_FIELD.get(field_name) or PARSERS_BY_TYPE[value_type]
    try:
        return Validation("valid", parser(raw_value), None)
    except ValueError as exc:
        return Validation("invalid", None, str(exc))
