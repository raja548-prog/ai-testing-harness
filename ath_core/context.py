"""Change context for LLM stages: the MR diff and the TSD, trimmed to what a failing test needs.

Selection is deterministic (word overlap with the failing case: name, class, tags, message), so the
same run always sends the same excerpt. Both inputs are masked before storage like everything else.

The TSD can come from a markdown file or from a Requirement-Harness SDLC package
(<rh-home>/packages/<ER>/manifest.json). From a package, ATH reads the approved TSD projections
and checks each file's SHA-256 against the hash recorded at approval, so a TSD edited after
sign-off is refused. Each TSD section keeps its `ⓘ <ids>` footer (R-, AC-, IMP- ids) for tracing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ath_core.schemas.atrf import ATRFCase

DIFF_CHARS = 8000
TSD_CHARS = 4000
_STOP = {"the", "and", "for", "with", "got", "expected", "was", "but", "not", "true", "false", "test", "java",
         "src", "main", "com", "should", "when", "then", "that", "this", "from", "into", "are", "has"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9]{2,}", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
    return {w for w in words if w not in _STOP}


def case_tokens(case: ATRFCase) -> set[str]:
    parts = [case.id, case.name, " ".join(case.tags)]
    if case.failure:
        parts += [case.failure.message, case.failure.trace[:1000]]
    return _tokens(" ".join(parts))


def split_diff(diff: str) -> list[tuple[str, str]]:
    """Unified diff -> [(path, text)] per file. Files start at `diff --git`, or at `---`/`+++` pairs."""
    lines = diff.splitlines(keepends=True)
    git = any(line.startswith("diff --git ") for line in lines)

    def starts(i: int) -> bool:
        if git:
            return lines[i].startswith("diff --git ")
        return lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")

    chunks: list[list[str]] = []
    for i, line in enumerate(lines):
        if starts(i) or not chunks:
            chunks.append([])
        chunks[-1].append(line)
    out = []
    for chunk in chunks:
        text = "".join(chunk)
        m = re.search(r"^\+\+\+ (?:b/)?(\S+)", text, re.M) or re.search(r"^diff --git a/(\S+)", text, re.M)
        if text.strip():
            out.append((m.group(1) if m else "", text))
    return out


def _pick(parts: list[tuple[str, str]], wanted: set[str], max_chars: int, what: str) -> str:
    ranked = sorted(enumerate(parts), key=lambda ip: (-len(_tokens(ip[1][0] + " " + ip[1][1]) & wanted), ip[0]))
    chosen, used = [], 0
    for _, (_, text) in ranked:
        if used + len(text) > max_chars and chosen:
            continue
        chosen.append(text[:max_chars])
        used += len(text)
    dropped = len(parts) - len(chosen)
    note = f"\n({dropped} more {what} not shown)" if dropped else ""
    return "".join(chosen).rstrip() + note


def diff_excerpt(diff: str, case: ATRFCase, max_chars: int = DIFF_CHARS) -> str:
    """The changed files most related to the failing case, within a size budget."""
    files = split_diff(diff)
    return _pick(files, case_tokens(case), max_chars, "changed file(s)") if files else ""


@dataclass
class TsdSection:
    title: str
    text: str


def tsd_sections(md: str) -> list[TsdSection]:
    sections: list[TsdSection] = []
    for line in md.splitlines(keepends=True):
        if re.match(r"#{1,4} ", line) or not sections:
            sections.append(TsdSection(line.lstrip("#").strip() if line.startswith("#") else "", ""))
        sections[-1].text += line
    return [s for s in sections if s.text.strip()]


def tsd_excerpt(md: str, case: ATRFCase, max_chars: int = TSD_CHARS) -> str:
    """The TSD sections most related to the failing case, footers (ⓘ ids) included."""
    secs = tsd_sections(md)
    return _pick([(s.title, s.text) for s in secs], case_tokens(case), max_chars, "TSD section(s)") if secs else ""


def read_rh_package(path: str | Path) -> str:
    """Approved TSD text from a Requirement-Harness package (manifest.json or its folder).

    Raises ValueError if the package has no TSD projection or a file no longer matches its approved hash.
    """
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    er_id = manifest.get("er_id", "")
    # <rh-home>/packages/<ER>/manifest.json; projection paths are relative to <rh-home>/er/<ER>/
    er_dir = manifest_path.resolve().parents[2] / "er" / er_id
    tsds = [p for p in manifest.get("projections", []) if str(p.get("kind", "")).upper().startswith("TSD")]
    if not tsds:
        raise ValueError(f"{manifest_path}: package has no TSD projection")
    parts = []
    for p in tsds:
        f = er_dir / p["path"]
        if not f.exists():
            raise ValueError(f"{manifest_path}: TSD projection {p['path']} not found under {er_dir}")
        body = f.read_bytes()
        if hashlib.sha256(body).hexdigest() != p.get("hash"):
            raise ValueError(f"{f} does not match the hash approved at {p.get('checkpoint', 'CP-TSD')}; "
                             f"it changed after sign-off")
        group = f" ({p['repo_group']})" if p.get("repo_group") else ""
        parts.append(f"<!-- {er_id} TSD{group}, approved hash {p['hash'][:12]} -->\n" + body.decode("utf-8"))
    return "\n\n".join(parts)
