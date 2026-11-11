"""Deterministic masking of payment and personal data.

Runs on every raw result and log excerpt before anything is stored as ATRF or
sent to a model. Masking is regex + checksum based, never model based.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

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


class Span(NamedTuple):
    """One masked region: original text [orig_start, orig_end) became masked text [masked_start, masked_end)."""
    orig_start: int
    orig_end: int
    masked_start: int
    masked_end: int
    category: str


def _common_prefix(a: str, b: str) -> str:
    return os.path.commonprefix([a, b])


def _compose(spans: list[Span], changes: list[tuple[int, int, int, str]]) -> None:
    """Fold one pass's changes into `spans`.

    Before the call, `spans` (earlier passes) and `changes` ((start, end, new_len, category), sorted,
    not overlapping) are both in positions of the text this pass ran on. Afterwards `spans` is in
    positions of the pass's output. A change that overlaps an earlier span absorbs it, so every span
    still maps back to a whole original region."""
    old = sorted(spans)

    def grow(s: Span) -> int:  # how much longer the masked region is than the original
        return (s.masked_end - s.masked_start) - (s.orig_end - s.orig_start)

    def to_orig(pos: int, edge: str) -> int:
        for s in old:
            if s.masked_start < pos < s.masked_end:  # inside an earlier region: take all of it
                return s.orig_start if edge == "start" else s.orig_end
        return pos - sum(grow(s) for s in old if s.masked_end <= pos)

    out: list[Span] = []
    absorbed: set[Span] = set()
    shift = 0  # growth of this pass's output before the current position
    for start, end, new_len, cat in changes:
        inside = [s for s in old if s.masked_start < end and s.masked_end > start]
        absorbed.update(inside)
        o_start = min([to_orig(start, "start")] + [s.orig_start for s in inside])
        o_end = max([to_orig(end, "end")] + [s.orig_end for s in inside])
        cats = "+".join(dict.fromkeys([*(s.category for s in inside), cat]))
        out.append(Span(o_start, o_end, start + shift, start + shift + new_len, cats))
        shift += new_len - (end - start)
    for s in old:
        if s in absorbed:
            continue
        moved = sum(n - (e - b) for b, e, n, _ in changes if e <= s.masked_start)
        out.append(s._replace(masked_start=s.masked_start + moved, masked_end=s.masked_end + moved))
    spans[:] = sorted(out, key=lambda s: s.masked_start)


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
        return self._run(text, None) if text else text

    def mask_spans(self, text: str) -> tuple[str, list[Span]]:
        """Mask, and say where: each Span maps a masked region of the output back to the original.

        Regions are as tight as the replacement allows (`cvv=123` -> `cvv=***` covers only `123`).
        Text outside every span is copied unchanged, so a position there maps by a fixed offset."""
        spans: list[Span] = []
        return (self._run(text, spans) if text else text), spans

    def _run(self, text: str, spans: list[Span] | None) -> str:
        cats = set(self.categories)
        passes: list[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]]] = []
        if "track2" in cats:
            passes.append(("track2", _TRACK2_RE, self._track2))
        if "pan" in cats:
            passes.append(("pan", _PAN_RE, self._pan))
        if "cvv" in cats:
            passes.append(("cvv", _CVV_RE, lambda m: (self._count("cvv"), f"{m.group(1)}{m.group(2)}***")[1]))
        # PIN blocks are always masked, whatever the profile says.
        passes.append(("pinblock", _PINBLOCK_RE,
                       lambda m: (self._count("pinblock"), f"{m.group(1)}{m.group(2)}[PINBLOCK]")[1]))
        if "aadhaar" in cats:
            passes.append(("aadhaar", _AADHAAR_RE, self._aadhaar))
        if "mobile" in cats:
            passes.append(("mobile", _MOBILE_RE, lambda m: (self._count("mobile"), "[MOBILE]")[1]))
        for cat, regex, fn in passes:
            if spans is None:
                text = regex.sub(fn, text)
                continue
            out, changes, pos = [], [], 0
            for m in regex.finditer(text):
                rep = fn(m)
                old = m.group(0)
                if rep == old:
                    continue
                pre = len(_common_prefix(old, rep))
                suf = len(_common_prefix(old[pre:][::-1], rep[pre:][::-1]))
                start, end = m.start() + pre, m.end() - suf
                out += [text[pos:start], rep[pre:len(rep) - suf]]
                changes.append((start, end, len(rep) - pre - suf, cat))
                pos = end
            if changes:
                text = "".join(out) + text[pos:]
                _compose(spans, changes)
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
