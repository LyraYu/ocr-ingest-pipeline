"""document_type from the concatenated page text (CLAUDE.md §6).

Every type's keywords live in KEYWORDS; adding a type is one entry here (plus its
rules in rules.py). Matching is case-insensitive on whole words, and a phrase may
be broken across lines. Each keyword carries a weight: title-like phrases
("medical certificate", "tax invoice") outweigh incidental ones ("gst" also shows
up in clinic letterheads). Highest total weight wins; a tie goes to the type
whose keyword appears first (titles sit near the top). No match →
UnsupportedDocumentTypeError.
"""

import re
from dataclasses import dataclass

from app.extraction import UnsupportedDocumentTypeError

KEYWORDS: dict[str, dict[str, int]] = {
    "medical_certificate": {
        "medical certificate": 3,
        "unfit for duty": 2,
        "medical leave": 2,
        "sick leave": 1,
    },
    "referral_letter": {
        "referral": 3,
        "referral letter": 2,
        "referring provider": 2,
        "kindly review": 1,
    },
    "receipt": {
        "receipt": 3,
        "tax invoice": 3,
        "official receipt": 2,
        "gst": 1,
    },
}


def _keyword_pattern(keyword: str) -> re.Pattern:
    words = [re.escape(w) for w in keyword.split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"\b", re.IGNORECASE)


_PATTERNS = {
    doc_type: {kw: (_keyword_pattern(kw), weight) for kw, weight in keywords.items()}
    for doc_type, keywords in KEYWORDS.items()
}


@dataclass(frozen=True)
class Classification:
    document_type: str
    score: int
    matched_keywords: tuple[str, ...]


def classify(text: str) -> Classification:
    candidates = []
    for doc_type, patterns in _PATTERNS.items():
        hits = {kw: (m.start(), weight) for kw, (p, weight) in patterns.items() if (m := p.search(text))}
        if hits:
            score = sum(weight for _, weight in hits.values())
            first = min(pos for pos, _ in hits.values())
            candidates.append((-score, first, doc_type, tuple(sorted(hits))))
    if not candidates:
        raise UnsupportedDocumentTypeError(
            f"no keyword of {sorted(KEYWORDS)} found in the document text"
        )
    neg_score, _, doc_type, matched = min(candidates)
    return Classification(doc_type, -neg_score, matched)
