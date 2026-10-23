"""Deterministic masking of payment and personal data.

Runs on every raw result and log excerpt before anything is stored as ATRF or
sent to a model. Masking is regex + checksum based, never model based.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Runs of 13+ digits, optionally separated by single spaces or dashes. Which part of a run is the PAN is
# decided in _pan, so a PAN followed by an expiry or amount, or behind a length prefix, is still found.
_PAN_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,}(?!\d)")
_DIGIT_GROUP = re.compile(r"\d+")
# Track 2: ;PAN=YYMM... or PAN=YYMM, with `=` or the `D` separator of EMV tag 57 / ISO 8583 field 35.
_TRACK2_RE = re.compile(r"(?i);?\d{12,19}[=d]\d{4}[0-9a-f]*\??")
# Key, then optional closing quote, then `:`/`=` (or whitespace), then an optional opening quote.
_KEY_SEP = r"[\"']?)(\s*[:=]\s*[\"']?|\s+)"
# CVV/CVC values next to a recognisable key: cvv=, "cvv": "…", card_cvv, cvvCode, securityCode, CVV 123.
_CVV_RE = re.compile(r"(?i)(?<![a-z])((?:card[_-]?)?(?:cvv2?|cvc2?|cid|card[_-]?verification|security[_-]?code)"
                     r"(?:[_-]?(?:code|value|number))?" + _KEY_SEP + r"(\d{3,4})")
# PIN block next to a key (16 hex chars).
_PINBLOCK_RE = re.compile(r"(?i)(?<![a-z])(pin[_ -]?(?:block|blk)" + _KEY_SEP + r"([0-9a-f]{16})")
# Aadhaar: 12 digits, first digit 2-9, optional space or dash in 4-4-4 groups, Verhoeff check digit.
# The checksum matters: RRNs are also 12 digits and are the main log correlation key.
_AADHAAR_RE = re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
# Indian mobile: optional +91/0 prefix, 10 digits starting 6-9.
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+91[ -]?|0)?[6-9]\d{9}(?!\d)")

CATEGORIES = ("pan", "cvv", "track2", "aadhaar", "mobile")


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(digits: str) -> bool:
    """Verhoeff checksum, used by Aadhaar numbers."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]  # int(): \d also matches non-ASCII digits
    return c == 0


def luhn_valid(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class Masker:
    """Masks the categories listed in a profile's `logs.masking`.

    `correlation_keys` (the product's declared keys, e.g. rrn, stan) exempt only the Aadhaar rule
    for a number written directly as that key's value (`rrn=…`, `rrn: …`, `"rrn": "…"`): RRNs are
    12 digits and about 1 in 10 pass the Verhoeff check by chance. Every other rule still applies.
    """
    categories: list[str] = field(default_factory=lambda: list(CATEGORIES))
    correlation_keys: list[str] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.categories = [c.strip().lower() for c in self.categories]
        if unknown := sorted(set(self.categories) - set(CATEGORIES)):
            raise ValueError(f"unknown masking categories {unknown}; known: {list(CATEGORIES)}")
        keys = "|".join(re.escape(k) for k in self.correlation_keys if k)
        self._key_value = re.compile(rf"(?i)\b(?:{keys})[\"']?\s*[:=]\s*[\"']?$") if keys else None

    def _count(self, cat: str) -> None:
        self.hits[cat] = self.hits.get(cat, 0) + 1

    def _aadhaar(self, m: re.Match) -> str:
        if not verhoeff_valid(re.sub(r"\D", "", m.group(0))):
            return m.group(0)  # e.g. an RRN: 12 digits, but not an Aadhaar number
        if self._key_value and self._key_value.search(m.string[max(0, m.start() - 40):m.start()]):
            return m.group(0)  # the value of a declared correlation key, e.g. rrn=…
        self._count("aadhaar")
        return "[AADHAAR]"

    def mask(self, text: str) -> str:
        if not text:
            return text
        cats = set(self.categories)
        if "track2" in cats:
            text = _TRACK2_RE.sub(self._track2, text)
        if "pan" in cats:
            text = _PAN_RE.sub(self._pan, text)
        if "cvv" in cats:
            text = _CVV_RE.sub(lambda m: (self._count("cvv"), f"{m.group(1)}{m.group(2)}***")[1], text)
        # PIN blocks are always masked, whatever the profile says.
        text = _PINBLOCK_RE.sub(lambda m: (self._count("pinblock"), f"{m.group(1)}{m.group(2)}[PINBLOCK]")[1], text)
        if "aadhaar" in cats:
            text = _AADHAAR_RE.sub(self._aadhaar, text)
        if "mobile" in cats:
            text = _MOBILE_RE.sub(lambda m: (self._count("mobile"), "[MOBILE]")[1], text)
        return text

    def _pan(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        groups = [(g.start(), g.end()) for g in _DIGIT_GROUP.finditer(raw)]
        out, pos, i = [], 0, 0
        while i < len(groups):
            found = self._pan_at(raw, groups, i)
            if found is None:
                i += 1
                continue
            start, end, i = found
            digits = re.sub(r"\D", "", raw[start:end])
            self._count("pan")
            out += [raw[pos:start], f"[PAN****{digits[-4:]}]"]
            pos = end
        return "".join(out) + raw[pos:]

    @staticmethod
    def _pan_at(raw: str, groups: list[tuple[int, int]], i: int) -> tuple[int, int, int] | None:
        """The longest Luhn-valid 13-19 digit PAN starting at digit group i, as (start, end, next group).

        Candidates are whole groups (`4111 1111 1111 1111 12/25` keeps the expiry) or, within one group,
        the digits behind an ISO 8583 LLVAR length prefix (`DE002=164111111111111111`).
        """
        for j in range(len(groups), i, -1):
            digits = re.sub(r"\D", "", raw[groups[i][0]:groups[j - 1][1]])
            if 13 <= len(digits) <= 19 and luhn_valid(digits):
                return groups[i][0], groups[j - 1][1], j
        start, end = groups[i]
        n = int(raw[start:start + 2]) if end - start >= 15 else 0
        if 13 <= n <= 19 and end - start >= 2 + n and luhn_valid(raw[start + 2:start + 2 + n]):
            return start + 2, start + 2 + n, i + 1
        return None

    def _track2(self, m: re.Match[str]) -> str:
        self._count("track2")
        return "[TRACK2]"
