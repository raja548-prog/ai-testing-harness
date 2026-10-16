"""Deterministic masking of payment and personal data.

Runs on every raw result and log excerpt before anything is stored as ATRF or
sent to a model. Masking is regex + checksum based, never model based.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 13-19 digits, optionally separated by single spaces or dashes.
_PAN_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
# Track 2: ;PAN=YYMM... or PAN=YYMM with service code (ISO 7813 shape).
_TRACK2_RE = re.compile(r";?\d{12,19}=\d{4}\d{3}\d*\??")
# CVV/CVC values next to a recognisable key.
_CVV_RE = re.compile(r"(?i)\b(cvv2?|cvc2?|cid|card_?verification)\b(\s*[:=]\s*\"?)(\d{3,4})")
# PIN block next to a key (16 hex chars).
_PINBLOCK_RE = re.compile(r"(?i)\b(pin_?block|pinblk)\b(\s*[:=]\s*\"?)([0-9a-f]{16})")
# Aadhaar: 12 digits, first digit 2-9, optional spaces in 4-4-4 groups.
_AADHAAR_RE = re.compile(r"(?<!\d)[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\d)")
# Indian mobile: optional +91/0 prefix, 10 digits starting 6-9.
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+91[ -]?|0)?[6-9]\d{9}(?!\d)")


def luhn_valid(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class Masker:
    """Masks the categories listed in a profile's `logs.masking`."""
    categories: list[str] = field(default_factory=lambda: ["pan", "cvv", "track2", "aadhaar", "mobile"])
    hits: dict[str, int] = field(default_factory=dict)

    def _count(self, cat: str) -> None:
        self.hits[cat] = self.hits.get(cat, 0) + 1

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
            text = _AADHAAR_RE.sub(lambda m: (self._count("aadhaar"), "[AADHAAR]")[1], text)
        if "mobile" in cats:
            text = _MOBILE_RE.sub(lambda m: (self._count("mobile"), "[MOBILE]")[1], text)
        return text

    def _pan(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            self._count("pan")
            return f"[PAN****{digits[-4:]}]"
        return raw

    def _track2(self, m: re.Match[str]) -> str:
        self._count("track2")
        return "[TRACK2]"
