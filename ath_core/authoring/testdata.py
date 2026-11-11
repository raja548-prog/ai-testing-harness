"""The approved test-data catalogue: the only source of card, account, mobile and Aadhaar values in tests.

The model never sees a value. It sees names, kinds, descriptions and field names, and writes a reference
such as `{{testdata:visa-credit-01.pan}}`; ATH fills in the value when it writes the file. Proposals and
patches keep the references.

    entries:
      - name: visa-credit-01
        kind: card
        description: Visa credit, domestic, active
        values: {pan: "4111111111111111", expiry: "12/30", cvv: "123"}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ath_core.masking.masker import luhn_valid, verhoeff_valid

KINDS = ("card", "account", "mobile", "aadhaar")
# The field each kind must have, and how it is checked.
REQUIRED = {"card": "pan", "account": "number", "mobile": "number", "aadhaar": "number"}
REF_RE = re.compile(r"\{\{\s*testdata:([A-Za-z0-9._-]+?)\.([A-Za-z0-9_-]+)\s*\}\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*$")


@dataclass
class Entry:
    name: str
    kind: str
    description: str
    values: dict[str, str]


@dataclass
class Catalogue:
    entries: dict[str, Entry] = field(default_factory=dict)
    source: Path | None = None

    def describe(self) -> str:
        """For the prompt: names, kinds, descriptions and field names. Never values."""
        if not self.entries:
            return "(no catalogue entries)"
        lines = ["Write a value as {{testdata:<name>.<field>}}; ATH fills it in. Entries:"]
        for e in sorted(self.entries.values(), key=lambda e: e.name):
            lines.append(f"- {e.name} ({e.kind}): {e.description or 'no description'}; "
                         f"fields: {', '.join(sorted(e.values))}")
        return "\n".join(lines)

    def unknown_refs(self, text: str) -> list[str]:
        out = []
        for m in REF_RE.finditer(text):
            entry = self.entries.get(m.group(1))
            if entry is None or m.group(2) not in entry.values:
                out.append(m.group(0))
        return sorted(set(out))

    def fill(self, text: str) -> str:
        """Replace every reference with its value. Raises KeyError for an unknown reference."""
        def value(m: re.Match[str]) -> str:
            entry = self.entries.get(m.group(1))
            if entry is None or m.group(2) not in entry.values:
                raise KeyError(f"unknown test-data reference {m.group(0)}")
            return entry.values[m.group(2)]
        return REF_RE.sub(value, text)


def _digits(v: str) -> str:
    return re.sub(r"[\s-]", "", v)


def catalogue_problems(data: object) -> list[str]:
    """Why a parsed catalogue file is not valid: shape, unique names, kinds, Luhn and Verhoeff checks."""
    raw = data.get("entries") if isinstance(data, dict) else data
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["the catalogue must be a list of entries, or a mapping with an `entries` list"]
    problems, seen = [], set()
    for i, e in enumerate(raw):
        where = f"entry {i + 1}"
        if not isinstance(e, dict):
            problems.append(f"{where}: not a mapping")
            continue
        name, kind, values = e.get("name"), e.get("kind"), e.get("values")
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{where}: name {name!r} must be letters, digits, '.', '_' or '-'")
            continue
        where = f"entry {name!r}"
        if name in seen:
            problems.append(f"{where}: duplicate name")
        seen.add(name)
        if kind not in KINDS:
            problems.append(f"{where}: kind {kind!r} is not one of {list(KINDS)}")
            continue
        if not isinstance(values, dict) or not values:
            problems.append(f"{where}: needs values")
            continue
        req = REQUIRED[kind]
        v = _digits(str(values.get(req, "")))
        if req not in values:
            problems.append(f"{where}: a {kind} needs a `{req}` value")
        elif kind == "card" and not (v.isdigit() and 13 <= len(v) <= 19 and luhn_valid(v)):
            problems.append(f"{where}: pan is not a 13-19 digit number that passes the Luhn check")
        elif kind == "aadhaar" and not (v.isdigit() and len(v) == 12 and v[0] in "23456789" and verhoeff_valid(v)):
            problems.append(f"{where}: number is not a 12-digit Aadhaar number that passes the Verhoeff check")
        elif kind == "mobile" and not re.fullmatch(r"(?:\+?91|0)?[6-9]\d{9}", v):
            problems.append(f"{where}: number is not a 10-digit Indian mobile number")
    return problems


def load_catalogue(path: str | Path | None) -> Catalogue:
    """Load and check the catalogue. No path: an empty catalogue. Raises ValueError when invalid."""
    if path is None:
        return Catalogue()
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if problems := catalogue_problems(data):
        raise ValueError(f"test-data catalogue {p}: " + "; ".join(problems))
    raw = (data.get("entries") if isinstance(data, dict) else data) or []
    entries = {e["name"]: Entry(name=e["name"], kind=e["kind"], description=str(e.get("description") or ""),
                                values={str(k): str(v) for k, v in e["values"].items()}) for e in raw}
    return Catalogue(entries=entries, source=p)
