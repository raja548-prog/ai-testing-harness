"""File formats for T3 edits: text find/replace, CSV/TSV row operations, JSON/YAML parse checks.

Every edit is worked out twice from the same base file: on the original text (what gets written) and on
the masked text the model saw (what goes into the reviewable patch). A `find` is only trusted when both
agree on where it is and it touches no masked region, so masked values are never sent back or overwritten.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Callable

import yaml

from ath_core.masking.masker import Span
from ath_core.schemas.authoring import Edit

_BOM = "﻿"

# What masking leaves behind. A `find`, `replace` or `content` holding one of these is refused:
# the model is copying a redacted value instead of writing a catalogue reference.
MASK_TOKEN_RE = re.compile(
    r"\[PAN\*{4}\d{0,4}\]|\[AADHAAR\]|\[MOBILE\]|\[TRACK2\]|\[PINBLOCK\]"
    # a masked CVV: the key, a separator, then *** (a bare *** is ordinary text, e.g. a comment banner)
    r"|(?i:(?<![a-z])(?:card[_-]?)?(?:cvv2?|cvc2?|cid|card[_-]?verification|security[_-]?code)"
    r"(?:[_-]?(?:code|value|number))?[\"']?(?:\s*[:=]\s*[\"']?|\s+)\*\*\*)")


class GuardError(ValueError):
    """A change breaks a rule. It is dropped with this message and never written."""


# ------------------------------------------------------------------------------------------- paths
def glob_regex(pattern: str) -> re.Pattern[str]:
    """`**/` matches zero or more folders, `**` anything, `*` and `?` stay inside one folder."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def glob_match(path: str, pattern: str) -> bool:
    return glob_regex(pattern).fullmatch(path) is not None


# ------------------------------------------------------------------------------------------- text
@dataclass
class TextFile:
    """A file as text: UTF-8, BOM removed, CRLF turned into LF. `encode` puts both back."""
    text: str
    bom: bool = False
    crlf: bool = False

    @classmethod
    def decode(cls, data: bytes) -> "TextFile | None":
        """None for a file that is not UTF-8 (it is neither sent nor edited)."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        bom = text.startswith(_BOM)
        text = text[1:] if bom else text
        crlf = "\r\n" in text
        return cls(text.replace("\r\n", "\n") if crlf else text, bom, crlf)

    def encode(self, text: str) -> bytes:
        if self.crlf:
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        return ((_BOM if self.bom else "") + text).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FileView:
    """One test file as ATH holds it during authoring: original and masked text, and how they map."""
    path: str
    kind: str
    format: str
    file: TextFile
    masked: str
    spans: list[Span]
    sha256: str  # of the bytes on disk
    key_column: str | None = None

    @property
    def raw(self) -> str:
        return self.file.text


def _occurrences(text: str, needle: str) -> list[int]:
    out, i = [], text.find(needle)
    while i >= 0:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def _to_orig(spans: list[Span], masked_pos: int) -> int:
    return masked_pos - sum((s.masked_end - s.masked_start) - (s.orig_end - s.orig_start)
                            for s in spans if s.masked_end <= masked_pos)


TOUCHES_MASKED = "edit touches masked data; edit by hand"


def locate_find(view: FileView, find: str) -> tuple[int, int]:
    """(original start, masked start) of a `find`, or GuardError.

    Accepted only when it holds no mask token, occurs exactly once in the masked text and once in the
    original, overlaps no masked region, and both occurrences are the same place."""
    if MASK_TOKEN_RE.search(find):
        raise GuardError(TOUCHES_MASKED)
    in_masked = _occurrences(view.masked, find)
    if not in_masked:
        raise GuardError("find text is not in the file as sent")
    if len(in_masked) > 1:
        raise GuardError(f"find text occurs {len(in_masked)} times in the file; it must occur exactly once")
    in_raw = _occurrences(view.raw, find)
    if len(in_raw) != 1:
        raise GuardError(TOUCHES_MASKED)
    ms, rs, n = in_masked[0], in_raw[0], len(find)
    for s in view.spans:
        if s.orig_start < rs + n and s.orig_end > rs or s.masked_start < ms + n and s.masked_end > ms:
            raise GuardError(TOUCHES_MASKED)
    if _to_orig(view.spans, ms) != rs:
        raise GuardError(TOUCHES_MASKED)
    return rs, ms


def apply_replace_edits(view: FileView, edits: list[Edit], fill: Callable[[str], str]) -> tuple[str, str]:
    """(new original text with catalogue values filled in, new masked text with references kept).

    Every find is matched against the base file, not against earlier edits; overlapping edits drop
    the whole change. Edits are applied together, from the end of the file backwards."""
    located = []
    for e in edits:
        if e.op != "replace":
            raise GuardError(f"{e.op} edits are for csv/tsv files; {view.path} is {view.format}")
        rs, ms = locate_find(view, e.find or "")
        located.append((rs, ms, len(e.find or ""), e.replace or ""))
    located.sort()
    for a, b in zip(located, located[1:]):
        if b[0] < a[0] + a[2]:
            raise GuardError("two edits overlap in the file; send them as one edit")
    raw, masked = view.raw, view.masked
    for rs, ms, n, rep in reversed(located):
        raw = raw[:rs] + fill(rep) + raw[rs + n:]
        masked = masked[:ms] + rep + masked[ms + n:]
    return raw, masked


# -------------------------------------------------------------------------------------------- csv
_DELIMS = (",", ";", "\t", "|")


@dataclass
class CsvLayout:
    delimiter: str
    quotechar: str
    quoting: int
    lines: list[str]  # the file split after each "\n"
    records: list[tuple[int, int, list[str]]] = field(default_factory=list)  # (first line, end line, fields)

    @property
    def header(self) -> list[str]:
        return next((r[2] for r in self.records if r[2]), [])

    def data_indices(self) -> list[int]:
        """Indices of non-blank records after the header."""
        first = next((i for i, r in enumerate(self.records) if r[2]), None)
        return [] if first is None else [i for i in range(first + 1, len(self.records)) if self.records[i][2]]

    def data_records(self) -> list[tuple[int, int, list[str]]]:
        return [self.records[i] for i in self.data_indices()]

    def render(self, fields: list[str]) -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=self.delimiter, quotechar=self.quotechar, quoting=self.quoting,
                   lineterminator="\n").writerow(fields)
        return buf.getvalue()


def _split_lines(text: str) -> list[str]:
    return [line for line in re.split(r"(?<=\n)", text) if line]


def _records(lines: list[str], delimiter: str, quotechar: str) -> list[tuple[int, int, list[str]]]:
    pos = [0]

    def feed():
        while pos[0] < len(lines):
            pos[0] += 1
            yield lines[pos[0] - 1]

    out, start = [], 0
    for row in csv.reader(feed(), delimiter=delimiter, quotechar=quotechar, strict=True):
        out.append((start, pos[0], row))
        start = pos[0]
    return out


def read_csv(text: str, fmt: str, like: CsvLayout | None = None) -> CsvLayout:
    """Parse a CSV/TSV file and work out its dialect from the file itself.

    The delimiter is tried in the order , ; tab | (tab only for tsv): the first giving more than one
    column and the same number of columns on every row wins (csv.Sniffer alone guesses wrong on small
    files). The quoting style is whichever of minimal or all-quoted reproduces most rows exactly."""
    lines = _split_lines(text)
    quotechar = like.quotechar if like else ("'" if text.count("'") > text.count('"') and
                                             re.search(r"(^|[,;\t|])'", text, re.M) else '"')
    candidates = [like.delimiter] if like else (["\t"] if fmt == "tsv" else list(_DELIMS))
    best: tuple[str, list] | None = None
    for d in candidates:
        try:
            recs = _records(lines, d, quotechar)
        except csv.Error:
            continue
        widths = {len(r[2]) for r in recs if r[2]}
        if len(widths) == 1 and widths.pop() > 1:
            best = (d, recs)
            break
        if best is None:
            best = (d, recs)
    if best is None:
        raise GuardError("file does not parse as CSV")
    layout = CsvLayout(best[0], quotechar, csv.QUOTE_MINIMAL, lines, best[1])
    if like:
        layout.quoting = like.quoting
        return layout
    votes = {csv.QUOTE_MINIMAL: 0, csv.QUOTE_ALL: 0}
    for start, end, row in (layout.data_records() or layout.records)[:20]:
        actual = "".join(lines[start:end]).rstrip("\n")
        for q in votes:
            layout.quoting = q
            votes[q] += layout.render(row).rstrip("\n") == actual
    layout.quoting = csv.QUOTE_ALL if votes[csv.QUOTE_ALL] > votes[csv.QUOTE_MINIMAL] else csv.QUOTE_MINIMAL
    return layout


def apply_row_edits(view: FileView, edits: list[Edit], fill: Callable[[str], str]) -> tuple[str, str]:
    """CSV/TSV row operations -> (new original text, new masked text).

    Columns an edit does not name keep their original values (so a masked PAN is never sent back or
    overwritten). Untouched lines stay byte-identical; a changed row is rewritten in place and an added
    row is appended, in the file's own delimiter, quote character and quoting style."""
    raw = read_csv(view.raw, view.format)
    masked = read_csv(view.masked, view.format, like=raw)
    if [(a, b) for a, b, _ in raw.records] != [(a, b) for a, b, _ in masked.records]:
        raise GuardError("masking changed the file's row layout; " + TOUCHES_MASKED)
    header = raw.header
    if not header:
        raise GuardError("file has no header row")
    if masked.header != header:
        raise GuardError("header row holds masked data; " + TOUCHES_MASKED)
    col = {name: i for i, name in enumerate(header)}
    updates: dict[int, tuple[list[str], list[str]]] = {}  # record index -> (raw fields, masked fields)
    added: list[tuple[list[str], list[str]]] = []
    data_idx = raw.data_indices()
    for e in edits:
        names = list((e.values or e.set or {}).keys())
        if e.op == "replace":
            raise GuardError(f"{view.path} is {view.format}: use add_row / update_row, not find/replace")
        if unknown := [n for n in names if n not in col]:
            raise GuardError(f"unknown column(s) {unknown}; the header is {header}")
        if e.op == "add_row":
            vals = e.values or {}
            added.append(([fill(vals.get(h, "")) for h in header], [vals.get(h, "") for h in header]))
            continue
        if not view.key_column:
            raise GuardError("update_row needs keyColumn on this source in the profile")
        if view.key_column not in col:
            raise GuardError(f"keyColumn {view.key_column!r} is not in the header {header}")
        k = col[view.key_column]
        hits = [i for i in data_idx if len(raw.records[i][2]) > k and raw.records[i][2][k] == e.key]
        if not hits:
            raise GuardError(f"no row has {view.key_column} = {e.key!r}")
        if len(hits) > 1:
            raise GuardError(f"{len(hits)} rows have {view.key_column} = {e.key!r}; the key must match exactly one")
        i = hits[0]
        if masked.records[i][2][k] != raw.records[i][2][k]:
            raise GuardError(f"the key column {view.key_column} holds masked data; " + TOUCHES_MASKED)
        if i in updates:
            raise GuardError(f"two edits change the row {view.key_column} = {e.key!r}; send them as one")
        r_fields, m_fields = list(raw.records[i][2]), list(masked.records[i][2])
        for name, value in (e.set or {}).items():
            r_fields[col[name]], m_fields[col[name]] = fill(value), value
        updates[i] = (r_fields, m_fields)

    def render(layout: CsvLayout, which: int) -> str:
        lines = list(layout.lines)
        for i in sorted(updates, reverse=True):
            start, end, _ = layout.records[i]
            line = layout.render(updates[i][which])
            last = layout.lines[end - 1]
            lines[start:end] = [line if last.endswith("\n") else line.rstrip("\n")]
        text = "".join(lines)
        if added:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "".join(layout.render(row[which]) for row in added)
        return text

    return render(raw, 0), render(masked, 1)


def check_new_csv(content: str, fmt: str, sibling: CsvLayout) -> None:
    """A new CSV must use the header and dialect of a sibling file in its folder."""
    layout = read_csv(content, fmt, like=sibling)
    if layout.header != sibling.header:
        raise GuardError(f"a new {fmt} file must use the header of the files next to it: {sibling.header}")
    width = len(sibling.header)
    if bad := [i + 1 for i, r in enumerate(layout.records) if r[2] and len(r[2]) != width]:
        raise GuardError(f"row(s) {bad[:5]} do not have {width} columns")


# ------------------------------------------------------------------------------------- json, yaml
def check_parses(fmt: str, text: str) -> None:
    """The edited file must still parse. The message gives a position only: the parser's own message
    can quote file content, which may hold a raw value."""
    try:
        if fmt == "json":
            json.loads(text)
        elif fmt == "yaml":
            yaml.safe_load(text)
    except json.JSONDecodeError as e:
        raise GuardError(f"the file no longer parses as JSON (line {e.lineno}, column {e.colno})")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise GuardError(f"the file no longer parses as YAML{where}")
