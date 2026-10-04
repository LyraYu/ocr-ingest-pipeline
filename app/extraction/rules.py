"""Field rules per document type (docs/DESIGN.md §6, §6.1) and the pure extractor.

FIELD_RULES[document_type] is a dict field_name → (regex, value_type). Its keys
are exactly the §6.1 field list for that type: every key gets an extracted_fields
row. The value is the first non-None capturing group of the first page whose text
matches (pages are searched in order; page text = lines joined with "\\n").

Labelled values: a label must start a line and be followed by ":" (value on the
same line or the next) or by a line break (label alone on its line, e.g. "TOTAL"
then "S$93.20"). Requiring line-start keeps prose such as "the above-named
patient is unfit" from matching the "Patient" label.
"""

import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field

_FLAGS = re.IGNORECASE | re.MULTILINE

# --- value patterns ------------------------------------------------------------

# Rest of the line, not empty and not itself a label ("NRIC/FIN:").
TEXT = r"(?![^\n]*:[ \t]*$)[^\n]*[^\s:]"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = (
    r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    rf"|\d{{1,2}}[ \t\-]+{_MONTH},?[ \t\-]+\d{{4}}"
    rf"|{_MONTH}[ \t]+\d{{1,2}},?[ \t]+\d{{4}})"
)
TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*[ap]\.?m\.?)?"
DATETIME = rf"{DATE}(?:[ \t]*(?:T|,|at)?[ \t]*{TIME})?"
_CURRENCY = r"(?:[A-Z]{1,3}\$|SGD|MYR|RM|PHP|USD|VND|₱|₫|\$)"
AMOUNT = rf"(?:{_CURRENCY}[ \t]*)?-?\d[\d,]*(?:\.\d+)?(?:[ \t]*{_CURRENCY})?"
NUMBER = r"-?\d+(?:\.\d+)?"
ICD10 = r"[A-Z]\d{2}(?:\.[0-9A-Z]{1,4})?"


def labelled(labels: str, value: str) -> str:
    """Regex source for `<label>:` + value on the same or next line. A space in
    `labels` (outside a [...] class) means one or more spaces/tabs."""
    labels = re.sub(r"\[[^\]]*\]| ", lambda m: r"[ \t]+" if m.group(0) == " " else m.group(0), labels)
    return rf"^[ \t]*(?:{labels})[ \t]*(?::[ \t]*(?:\n[ \t]*)?|\n[ \t]*)({value})"


def _rule(source: str, value_type: str) -> tuple[re.Pattern, str]:
    return re.compile(source, _FLAGS), value_type


# --- labels shared across types --------------------------------------------------

CLAIMANT_NAME = labelled(
    r"Patient's Name|Patient Name|Name of Patient|Claimant Name|Claimant|Member Name"
    r"|Employee Name|Re:[ \t]*Patient|Patient|Name",
    TEXT,
)
CLAIMANT_ADDRESS = labelled(r"(?:Home |Residential |Mailing |Patient )?Address", TEXT)
CLAIMANT_DOB = labelled(r"Date of Birth|Birth Date|D\.?O\.?B\.?", DATE)
# A provider stated explicitly. Without one, extract() falls back to the first line
# of page 1 (the letterhead).
PROVIDER_NAME = labelled(
    r"Referring (?:Provider|Clinic)|Provider Name|Provider|Clinic Name|Issuing Clinic|Issued by",
    TEXT,
)

FIELD_RULES: dict[str, dict[str, tuple[re.Pattern, str]]] = {
    "referral_letter": {
        "claimant_name": _rule(CLAIMANT_NAME, "text"),
        "provider_name": _rule(PROVIDER_NAME, "text"),
        "signature_presence": _rule(r"\bsignature\b|\[signed\]", "bool"),
        "total_amount_paid": _rule(
            labelled(r"Total Amount Paid|Total Paid|Amount Paid|Paid Amount", AMOUNT), "amount"
        ),
        "total_approved_amount": _rule(
            labelled(r"Total Approved Amount|Total Amount Approved|Approved Amount|Amount Approved", AMOUNT),
            "amount",
        ),
        "total_requested_amount": _rule(
            labelled(r"Total Requested Amount|Total Amount Requested|Requested Amount|Amount Requested", AMOUNT),
            "amount",
        ),
    },
    "medical_certificate": {
        "claimant_name": _rule(CLAIMANT_NAME, "text"),
        "claimant_address": _rule(CLAIMANT_ADDRESS, "text"),
        "claimant_date_of_birth": _rule(CLAIMANT_DOB, "date"),
        "diagnosis_name": _rule(labelled(r"(?:Provisional |Final |Primary )?Diagnosis", TEXT), "text"),
        "discharge_date_time": _rule(
            labelled(
                r"Discharge Date[ \t]*(?:/|&|and)[ \t]*Time|Date[ \t]*/[ \t]*Time of Discharge"
                r"|Discharge Date|Discharged (?:on|at)",
                DATETIME,
            ),
            "datetime",
        ),
        "icd_code": _rule(labelled(r"ICD(?:[ \t]*-?[ \t]*10)?(?: Code)?|Diagnosis Code", ICD10), "text"),
        "provider_name": _rule(PROVIDER_NAME, "text"),
        "submission_date_time": _rule(
            labelled(
                r"Submission Date[ \t]*(?:/|&|and)[ \t]*Time|Date[ \t]*/[ \t]*Time of Submission"
                r"|Submission Date|Submitted (?:on|at)",
                DATETIME,
            ),
            "datetime",
        ),
        "date_of_mc": _rule(
            labelled(r"Date of MC|MC Date|Date of Medical Certificate|Date of Issue|Issue Date|Date Issued", DATE),
            "date",
        ),
        "mc_days": _rule(
            labelled(r"MC Days|No\.? of Days|Number of Days|Days of (?:MC|Medical Leave|Sick Leave)", NUMBER)
            + rf"|\bperiod[ \t]+of[ \t]+({NUMBER})[ \t]*(?:\(\w+\)[ \t]*)?day"
            + rf"|\b({NUMBER})[ \t]*day(?:\(s\)|s)?[ \t]+(?:of[ \t]+)?(?:medical|sick)[ \t]+leave",
            "int",
        ),
    },
    "receipt": {
        "claimant_name": _rule(CLAIMANT_NAME, "text"),
        "claimant_address": _rule(CLAIMANT_ADDRESS, "text"),
        "claimant_date_of_birth": _rule(CLAIMANT_DOB, "date"),
        "provider_name": _rule(PROVIDER_NAME, "text"),
        "tax_amount": _rule(
            labelled(r"(?:GST|VAT|SST|Tax)(?: Amount)?(?:[ \t]*@?[ \t]*\(?[ \t]*\d+(?:\.\d+)?[ \t]*%[ \t]*\)?)?", AMOUNT),
            "amount",
        ),
        "total_amount": _rule(
            labelled(
                r"(?:Grand |Net |Nett )?Total(?: Amount)?(?: Payable| Due)?(?:[ \t]*\((?:incl|including)[^)\n]*\))?"
                r"|Amount Due|Amount Payable",
                AMOUNT,
            ),
            "amount",
        ),
    },
}

PROVIDER_EXCLUDE = re.compile(r"fullerton\s+health", re.IGNORECASE)


# --- extractor -----------------------------------------------------------------


@dataclass(frozen=True)
class SourceLine:
    id: Hashable
    text: str


@dataclass(frozen=True)
class RawField:
    field_name: str
    value_type: str
    raw_value: str | None
    source_line_ids: list = field(default_factory=list)


def _page_text(lines: Sequence[SourceLine]) -> tuple[str, list[tuple[int, int, Hashable]]]:
    """Lines joined with "\\n", plus each line's (start, end, id) offsets in that text."""
    spans, offset = [], 0
    for line in lines:
        spans.append((offset, offset + len(line.text), line.id))
        offset += len(line.text) + 1
    return "\n".join(line.text for line in lines), spans


def _overlapping(spans, start: int, end: int) -> list:
    return [line_id for s, e, line_id in spans if s < end and e > start]


def _find(pattern: re.Pattern, pages) -> tuple[str, list] | None:
    for text, spans in pages:
        m = pattern.search(text)
        if m:
            value = next((g for g in m.groups() if g is not None), m.group(0))
            return value.strip(), _overlapping(spans, m.start(), m.end())
    return None


def _default_provider(first_page: Sequence[SourceLine]) -> tuple[str, list] | None:
    """First non-empty line of page 1 (the letterhead), skipping any line that only
    mentions the Fullerton Health network (footers, panel notes)."""
    for line in first_page:
        if line.text.strip() and not PROVIDER_EXCLUDE.search(line.text):
            return line.text.strip(), [line.id]
    return None


def extract(document_type: str, pages: Sequence[Sequence[SourceLine]]) -> list[RawField]:
    """One RawField per field of `document_type` (raw_value None when not found)."""
    rules = FIELD_RULES[document_type]
    page_texts = [_page_text(lines) for lines in pages]
    fields = []
    for name, (pattern, value_type) in rules.items():
        found = _find(pattern, page_texts)
        if found is None and name == "provider_name" and pages:
            found = _default_provider(pages[0])
        raw, line_ids = found if found else (None, [])
        fields.append(RawField(name, value_type, raw, line_ids))
    return fields
