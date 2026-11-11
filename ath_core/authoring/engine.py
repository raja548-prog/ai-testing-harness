"""T3 authoring engine: one model call turns TSD clauses into proposed test changes, checked by ATH.

Used by `ainxt-ath author` (standalone, no run) and by stage T3 of a run. Steps:
  1. gather context: one masked, size-capped <data> block per input, selected deterministically
  2. one call to skill t03-author (a single author; a human reviews the result)
  3. parse its JSON change by change
  4. guards: a change that breaks a rule is dropped with the reason, never written
  5. review flags: weakened tests, new imports, risky API calls (they do not drop the change)
  6. the proposal (with its hash), rendered as changes.patch and checklist.md from masked text
Applying (step 7) is the TestAuthor adapter's job (ath_core/adapters/author/local_fs.py); it re-runs
the same `Workspace.realize` checks against the files as they are then.
"""
from __future__ import annotations

import difflib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ath_core.authoring.flags import assertion_lines, review_flags
from ath_core.authoring.formats import (
    MASK_TOKEN_RE, FileView, GuardError, TextFile, apply_replace_edits, apply_row_edits, check_new_csv,
    check_parses, glob_match, read_csv, sha256_bytes,
)
from ath_core.authoring.signatures import signatures
from ath_core.authoring.testdata import Catalogue, load_catalogue
from ath_core.context import (
    load_product_skill, pick, rank, select_tsd_sections, split_diff, tokens, tsd_clause_ids, tsd_sections,
)
from ath_core.masking import CATEGORIES, Masker
from ath_core.schemas.authoring import (
    AcceptedChange, Change, DroppedChange, Proposal, parse_output,
)
from ath_core.schemas.profile import TestProfile, TestSource

SKILL = "t03-author"
PLACEHOLDER = "(not attached)"
BLOCKS = ("tsd", "tests", "tree", "conventions", "testdata", "product", "code", "templates", "diff", "impact",
          "outdated")
_WHAT = {"pan": "a card number", "cvv": "a card security code", "track2": "card track data",
         "aadhaar": "an Aadhaar number", "mobile": "a mobile number", "pinblock": "a PIN block"}


class AuthorAgent(Protocol):
    model: str
    last_call: dict[str, Any]

    def run(self, blocks: dict[str, str]) -> dict[str, Any]: ...


@dataclass
class AuthorInputs:
    tsd: str
    tests_dir: Path
    sections: list[str] = field(default_factory=list)  # TSD section titles or ⓘ ids; empty = all
    code_dir: Path | None = None
    diff: str | None = None
    outdated: list[dict[str, str]] | None = None  # [{id, suite, name, message}] marked test-outdated by triage
    impact: str | None = None


def authoring_masker(profile: TestProfile) -> Masker:
    """Every category, whatever the profile's `logs.masking` says: test files are not logs, and a
    category switched off for logs must not let raw test data reach the model."""
    return Masker(categories=list(CATEGORIES), correlation_keys=profile.correlation_keys())


@dataclass
class Realized:
    source: TestSource
    new_bytes: bytes
    masked_old: str  # "" for a create
    masked_new: str  # with catalogue references, never values
    base_sha256: str | None


def _walk(root: Path) -> list[str]:
    """Every file under root as a sorted posix path; hidden folders (.git) skipped, symlinked folders not followed."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        out += [(rel / f).as_posix() for f in filenames if not f.startswith(".")]
    return sorted(out)


class Workspace:
    """The test checkout as T3 sees it: which files are test files, how they look masked, and what a
    change would turn them into."""

    def __init__(self, profile: TestProfile, tests_dir: str | Path):
        if profile.test_authoring is None:
            raise ValueError(f"profile {profile.product} has no testAuthoring section")
        self.profile = profile
        self.ta = profile.test_authoring
        self.root = Path(tests_dir).resolve()
        if not self.root.is_dir():
            raise ValueError(f"test checkout {tests_dir} is not a folder")
        self.masker = authoring_masker(profile)
        self.catalogue: Catalogue = load_catalogue(self.root / self.ta.test_data) if self.ta.test_data else Catalogue()
        self._views: dict[str, FileView | None] = {}
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            self._files = _walk(self.root)
        return self._files

    def source_for(self, rel: str) -> TestSource | None:
        return next((s for s in self.ta.sources if any(glob_match(rel, g) for g in s.paths)), None)

    def test_files(self) -> list[str]:
        return [f for f in self.files() if self.source_for(f)]

    def check_path(self, rel: str) -> tuple[Path, TestSource]:
        """The file a change targets, or GuardError: relative, no `..`, inside the checkout after resolving
        symlinks, and matching a `sources` glob (so never source, build or CI files)."""
        pure = PurePosixPath(rel)
        if not rel or "\\" in rel or pure.is_absolute() or ".." in pure.parts or pure.as_posix() != rel:
            raise GuardError("path must be relative to the test checkout, in / form, without '..'")
        if any(part.startswith(".") for part in pure.parts):
            raise GuardError("path is inside a hidden folder or names a hidden file")
        source = self.source_for(rel)
        if source is None:
            raise GuardError("not a test file: the path matches no testAuthoring.sources glob")
        target = self.root / rel
        if not target.resolve().is_relative_to(self.root):
            raise GuardError("path leaves the test checkout (through a symlink)")
        return target, source

    def view(self, rel: str) -> FileView | None:
        """The file as the model sees it; None if it is not UTF-8."""
        if rel not in self._views:
            target, source = self.check_path(rel)
            data = target.read_bytes()
            tf = TextFile.decode(data)
            if tf is None:
                self._views[rel] = None
            else:
                masked, spans = self.masker.mask_spans(tf.text)
                self._views[rel] = FileView(rel, source.kind, source.edit_format, tf, masked, spans,
                                            sha256_bytes(data), source.key_column)
        return self._views[rel]

    def check_written(self, text: str) -> None:
        """Text the model wrote: no mask tokens, only known catalogue references, nothing Masker would mask."""
        if m := MASK_TOKEN_RE.search(text):
            raise GuardError(f"model wrote the mask token {m.group(0)}; mask tokens are redacted values, "
                             f"never literals: use a testData reference")
        if bad := self.catalogue.unknown_refs(text):
            raise GuardError(f"unknown test-data reference(s) {bad}")
        masked, spans = Masker(categories=list(CATEGORIES)).mask_spans(text)
        if spans:
            found = []
            for s in spans:
                cat = s.category.split("+")[0]
                found.append(f"{_WHAT.get(cat, cat)} ({masked[s.masked_start:s.masked_end]})")
            raise GuardError(f"model wrote a value that looks like {', '.join(dict.fromkeys(found))}; "
                             f"use a testData reference or another value")

    def _written_parts(self, change: Change | AcceptedChange) -> list[str]:
        parts = [change.content or ""]
        for e in change.edits:
            # `key` too: it is stored in the proposal, so it must not carry a raw value either
            parts += [e.replace or "", e.key or "", *(e.values or {}).values(), *(e.set or {}).values()]
        return parts

    def _sibling_layout(self, target: Path, rel: str, source: TestSource):
        """Header and dialect of the first CSV of the same source in the new file's folder."""
        folder = PurePosixPath(rel).parent.as_posix()
        siblings = [f for f in self.files() if PurePosixPath(f).parent.as_posix() == folder
                    and f != rel and self.source_for(f) == source]
        for f in siblings:
            p = self.root / f
            tf = TextFile.decode(p.read_bytes())
            if tf is not None:
                return read_csv(tf.text, source.edit_format)
        raise GuardError(f"a new {source.edit_format} file needs a sibling {source.edit_format} file in "
                         f"{PurePosixPath(rel).parent}/ to copy its header and dialect from")

    def realize(self, change: Change | AcceptedChange, editable: set[str] | None) -> Realized:
        """What the change turns the file into, or GuardError. `editable`: files sent whole to the model
        (None skips that check, at apply time, where the base-file hash already pins the file)."""
        target, source = self.check_path(change.path)
        fmt = source.edit_format
        parts = self._written_parts(change)
        if sum(len(p) for p in parts) > self.ta.limits.max_change_chars:
            raise GuardError(f"change is over the limit of {self.ta.limits.max_change_chars} characters")
        for text in parts:
            self.check_written(text)
        fill = self.catalogue.fill
        if change.action == "create":
            if target.exists() or target.is_symlink():
                raise GuardError("create is only for a file that does not exist; use update")
            if change.edits or change.content is None:
                raise GuardError("create takes the whole file as content, and no edits")
            if fmt in ("csv", "tsv"):
                check_new_csv(change.content, fmt, self._sibling_layout(target, change.path, source))
            raw_new = fill(change.content)
            check_parses(fmt, raw_new)
            return Realized(source, raw_new.encode("utf-8"), "", change.content, None)
        if not target.is_file():
            raise GuardError("update is only for a file that exists; use create")
        if editable is not None and change.path not in editable:
            raise GuardError("the file was not sent whole to the model, so it cannot be updated")
        if change.content is not None or not change.edits:
            raise GuardError("update takes edits, not content")
        for e in change.edits:
            if problem := e.problem():
                raise GuardError(problem)
        view = self.view(change.path)
        if view is None:
            raise GuardError("file is not UTF-8, so it cannot be updated")
        if fmt in ("csv", "tsv"):
            raw_new, masked_new = apply_row_edits(view, change.edits, fill)
        else:
            raw_new, masked_new = apply_replace_edits(view, change.edits, fill)
        check_parses(fmt, raw_new)
        if raw_new == view.raw:
            raise GuardError("the edits change nothing")
        return Realized(source, view.file.encode(raw_new), view.masked, masked_new, view.sha256)


# ------------------------------------------------------------------------------------------ context
@dataclass
class Gathered:
    blocks: dict[str, str]
    editable: set[str]   # files sent whole: the only ones an update may touch
    tsd_sent: str
    notes: list[str] = field(default_factory=list)
    product_skill_version: str = ""


def _cap(text: str, limit: int, what: str = "characters") -> str:
    return text if len(text) <= limit else text[:limit] + f"\n({len(text) - limit} more {what} not shown)"


def _file_block(view: FileView) -> str:
    return (f"===== file: {view.path} (kind: {view.kind}, format: {view.format}, editable: true) =====\n"
            f"{view.masked.rstrip(chr(10))}\n===== end of file: {view.path} =====\n")


def _as_glob(path: str) -> str:
    """A module path as a glob: a folder (no extension, or a trailing /) covers everything under it."""
    p = path.strip().lstrip("./")
    if any(ch in p for ch in "*?"):
        return p
    if p.endswith("/") or "." not in PurePosixPath(p).name:
        return p.rstrip("/") + "/**"
    return p


def module_globs(product_skill_text: str) -> list[str]:
    """Paths from the product skill's module table (`| Module | Path | Responsibility |`), as globs."""
    globs: list[str] = []
    col: int | None = None  # index of the Path column while inside the module table
    for line in product_skill_text.splitlines():
        if not line.strip().startswith("|"):
            col = None
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "module" in lowered and "path" in lowered:
            col = lowered.index("path")
        elif col is not None and col < len(cells):
            cell = cells[col]
            if cell and not set(cell) <= set("-: ") and not cell.startswith("<"):
                globs.append(_as_glob(cell))
    return globs


class AuthoringEngine:
    def __init__(self, profile: TestProfile, agent: AuthorAgent | None = None):
        if profile.test_authoring is None:
            raise ValueError(f"profile {profile.product} has no testAuthoring section")
        self.profile = profile
        self.ta = profile.test_authoring
        self.agent = agent

    def gather(self, ws: Workspace, inputs: AuthorInputs) -> Gathered:
        lim, mask = self.ta.limits, ws.masker.mask
        notes: list[str] = []
        blocks = dict.fromkeys(BLOCKS, PLACEHOLDER)

        # tsd: chosen sections, whole sections up to the cap, ⓘ footers kept
        selected = mask(select_tsd_sections(inputs.tsd, inputs.sections))
        secs = tsd_sections(selected)
        sent, used = [], 0
        for s in secs:
            if used + len(s.text) > lim.tsd_chars and sent:
                break
            sent.append(s.text[:lim.tsd_chars])
            used += len(s.text)
        tsd_sent = "".join(sent).rstrip()
        if len(sent) < len(secs):
            tsd_sent += f"\n({len(secs) - len(sent)} more TSD section(s) not shown)"
        if not tsd_sent.strip():
            raise ValueError("the TSD is empty")
        blocks["tsd"] = tsd_sent

        wanted = tokens(tsd_sent + " " + " ".join(
            f"{c.get('id', '')} {c.get('name', '')} {c.get('message', '')}" for c in inputs.outdated or []))

        # tests: whole files or none, ranked by overlap with the TSD (and outdated cases)
        views, unreadable = [], []
        for rel in ws.test_files():
            try:
                v = ws.view(rel)
            except GuardError as e:  # e.g. a symlink that leads out of the checkout
                notes.append(f"{rel} not sent: {e}")
                continue
            (views.append(v) if v is not None else unreadable.append(rel))
        chosen, used = [], 0
        for i in rank([(v.path, v.masked) for v in views], wanted):
            text = _file_block(views[i])
            if len(chosen) >= lim.test_files or used + len(text) > lim.tests_chars:
                continue
            chosen.append(views[i])
            used += len(text)
        chosen.sort(key=lambda v: v.path)
        left = len(views) - len(chosen)
        body = "".join(_file_block(v) for v in chosen) or "(no test files match testAuthoring.sources yet)\n"
        if left:
            body += f"({left} more test file(s) not shown: they are listed in `tree` and cannot be updated)\n"
        if unreadable:
            body += f"({len(unreadable)} file(s) are not UTF-8 and were not sent: {', '.join(unreadable[:10])})\n"
        blocks["tests"] = body.rstrip()

        # tree: paths only, test files first
        tests = set(ws.test_files())
        paths = [p for p in ws.files() if p in tests] + [p for p in ws.files() if p not in tests]
        tree = "\n".join(paths[:lim.tree_paths])
        if len(paths) > lim.tree_paths:
            tree += f"\n({len(paths) - lim.tree_paths} more path(s) not shown)"
        blocks["tree"] = mask(tree) or "(empty checkout)"

        if self.ta.conventions:
            text = self.profile.resolve_path(self.ta.conventions).read_text(encoding="utf-8")
            blocks["conventions"] = _cap(mask(text), lim.conventions_chars)
        if self.ta.test_data:
            blocks["testdata"] = mask(ws.catalogue.describe())
        product = load_product_skill(self.profile, lim.product_chars)
        if product:
            blocks["product"] = mask(product.text)

        if inputs.code_dir:
            blocks["code"] = self._code_block(Path(inputs.code_dir), product.text if product else "", wanted,
                                              mask, notes)
        if self.ta.templates:
            blocks["templates"] = self._templates_block(ws, tsd_sent, chosen, wanted, mask)
        if inputs.diff:
            files = split_diff(inputs.diff)
            if files:
                blocks["diff"] = mask(pick(files, wanted, lim.diff_chars, "changed file(s)"))
        if inputs.impact:
            blocks["impact"] = mask(inputs.impact)
        if inputs.outdated:
            lines = [f"- {c.get('id', '')} ({c.get('suite', '')}): {c.get('name', '')}\n  failure: "
                     f"{' '.join(str(c.get('message', '')).split())[:500]}" for c in inputs.outdated]
            blocks["outdated"] = _cap(mask("\n".join(lines)), lim.outdated_chars)
        return Gathered(blocks, {v.path for v in chosen}, tsd_sent, notes, product.version if product else "")

    def _code_block(self, code_dir: Path, product_text: str, wanted: set[str], mask, notes: list[str]) -> str:
        root = code_dir.resolve()
        if not root.is_dir():
            raise ValueError(f"code checkout {code_dir} is not a folder")
        globs = self.ta.code_context or module_globs(product_text)
        if not globs:
            return "(no codeContext globs, and the product skill's module table names no paths)"
        files = [f for f in _walk(root) if any(glob_match(f, g) for g in globs)]
        parts = []
        for rel in files:
            tf = TextFile.decode((root / rel).read_bytes())
            if tf is None:
                continue
            sig, note = signatures(rel, tf.text)
            if note:
                notes.append(note)
            if sig.strip():
                parts.append((rel, f"// {rel}\n{sig}\n" + (f"// note: {note}\n" if note else "")))
        if not parts:
            return f"(no source files match {globs})"
        return mask(pick(parts, wanted, self.ta.limits.code_chars, "source file(s)"))

    def _templates_block(self, ws: Workspace, tsd: str, sent: list[FileView], wanted: set[str], mask) -> str:
        files = [f for f in ws.files() if any(glob_match(f, g) for g in self.ta.templates)]
        if not files:
            return f"(no files match templates {self.ta.templates})"
        haystack = (tsd + "\n" + "\n".join(v.masked for v in sent if v.kind == "data")).lower()
        named = [f for f in files if re.search(rf"\b{re.escape(PurePosixPath(f).stem.lower())}\b", haystack)
                 or re.search(rf"\b{re.escape(PurePosixPath(f).stem.lower().removesuffix('-template'))}\b", haystack)]
        if not named:
            return ("(no template is named by the TSD or by the scenario files sent; available: "
                    + ", ".join(PurePosixPath(f).name for f in files[:30]) + ")")
        parts = []
        for f in named:
            tf = TextFile.decode((ws.root / f).read_bytes())
            if tf is not None:
                parts.append((f, f"===== template: {f} =====\n{tf.text.rstrip()}\n"))
        return mask(pick(parts, wanted, self.ta.limits.templates_chars, "template(s)"))

    # ---------------------------------------------------------------------------------- proposal
    def propose(self, inputs: AuthorInputs, source: str, proposal_id: str | None = None) -> "Drafted":
        """Gather, call the model once, check every change. Raises ValueError for unusable input and
        the agent's own error (AgentError) when the call fails."""
        if self.agent is None:
            raise ValueError("no author agent configured")
        ws = Workspace(self.profile, inputs.tests_dir)
        g = self.gather(ws, inputs)
        try:
            out = self.agent.run(g.blocks)
        finally:
            call = dict(getattr(self.agent, "last_call", {}) or {})
        parsed = parse_output(out)
        # Free text the model wrote is stored and shown: mask it like everything else.
        m = ws.masker.mask
        parsed.summary = m(parsed.summary)
        for q in parsed.spec_questions:
            q.clause, q.question = m(q.clause), m(q.question)
        for ch in parsed.changes:
            ch.reason, ch.clauses = m(ch.reason), [m(c) for c in ch.clauses]
        ids = tsd_clause_ids(g.tsd_sent)
        titles = {s.title.casefold(): s.title for s in tsd_sections(g.tsd_sent) if s.title}
        accepted: list[AcceptedChange] = []
        rendered: list[tuple[AcceptedChange, str, str]] = []
        dropped = [DroppedChange(action="?", path=m(p), reason=m(why)) for p, why in parsed.malformed]
        for ch in parsed.changes:
            try:
                if len(accepted) >= self.ta.limits.max_changes:
                    raise GuardError(f"over the limit of {self.ta.limits.max_changes} changed files per proposal")
                if any(a.path == ch.path for a in accepted):
                    raise GuardError("the same file is changed twice; send one change per file")
                traced_by = _trace(ch.clauses, ids, titles)
                r = ws.realize(ch, editable=g.editable)
            except GuardError as e:
                dropped.append(DroppedChange(action=ch.action, path=m(ch.path), reason=m(str(e)), clauses=ch.clauses))
                continue
            acc = AcceptedChange(
                action=ch.action, path=ch.path, kind=r.source.kind, format=r.source.edit_format,
                clauses=ch.clauses, traced_by=traced_by, reason=ch.reason, content=ch.content, edits=ch.edits,
                base_sha256=r.base_sha256, result_sha256=sha256_bytes(r.new_bytes),
                flags=review_flags(r.masked_old, r.masked_new, r.source.kind))
            accepted.append(acc)
            rendered.append((acc, r.masked_old, r.masked_new))
        proposal = Proposal(
            id=proposal_id or f"author-{uuid.uuid4().hex[:10]}", product=self.profile.product, source=source,
            mode=self.ta.mode, model=getattr(self.agent, "model", ""), skill=SKILL,
            product_skill_version=g.product_skill_version,
            tsd_sha256=sha256_bytes(g.tsd_sent.encode("utf-8")), prompt_sha256=str(call.get("prompt_sha256", "")),
            blocks={k: len(v) for k, v in g.blocks.items()}, changes=accepted, dropped=dropped,
            spec_questions=parsed.spec_questions, summary=parsed.summary, agent_call=call,
            proposed_count=len(parsed.changes) + len(parsed.malformed))
        return Drafted(proposal, render_patch(rendered), render_checklist(proposal, rendered, g.notes))


def _trace(clauses: list[str], ids: set[str], titles: dict[str, str]) -> str:
    """Every cited clause must be in the TSD sent: ⓘ ids when it has any, else section titles."""
    clauses = [c.strip() for c in clauses if c.strip()]
    if not clauses:
        raise GuardError("cites no TSD clause; every change must trace to the TSD")
    if ids:
        bad = [c for c in clauses if c not in ids]
        if bad:
            hint = " (cite the ⓘ ids, not section titles)" if any(c.casefold() in titles for c in bad) else ""
            raise GuardError(f"clause(s) {bad} are not ⓘ ids of the TSD sent {sorted(ids)}{hint}")
        return "id"
    bad = [c for c in clauses if c.casefold() not in titles]
    if bad:
        raise GuardError(f"clause(s) {bad} are not section titles of the TSD sent")
    return "title"


@dataclass
class Drafted:
    proposal: Proposal
    patch: str
    checklist: str


# ---------------------------------------------------------------------------------------- rendering
def _lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def render_patch(rendered: list[tuple[AcceptedChange, str, str]]) -> str:
    """Unified diff of the masked base text: for review, not for `git apply` (apply goes through ATH)."""
    out = []
    for acc, old, new in rendered:
        a = "/dev/null" if acc.action == "create" else f"a/{acc.path}"
        out += difflib.unified_diff(_lines(old), _lines(new), fromfile=a, tofile=f"b/{acc.path}")
    return "".join(out)


def render_checklist(p: Proposal, rendered: list[tuple[AcceptedChange, str, str]], notes: list[str]) -> str:
    lines = [f"# Test authoring checklist: {p.id}", "",
             f"Product `{p.product}` · source `{p.source}` · model `{p.model or '(none)'}` · skill `{p.skill}`"
             + (f" · product skill v{p.product_skill_version}" if p.product_skill_version else ""), "",
             "Values from the test-data catalogue appear as `{{testdata:...}}` references; existing test data "
             "is shown masked.", ""]
    lines += [f"## Changes ({len(p.changes)})", ""]
    if not p.changes:
        lines += ["None.", ""]
    for acc, old, new in rendered:
        lines.append(f"### {acc.action} `{acc.path}` ({acc.kind}, {acc.format})")
        trace = ", ".join(acc.clauses) + (" (traced by section title only)" if acc.traced_by == "title" else "")
        lines.append(f"- Clauses: {trace}")
        if acc.reason:
            lines.append(f"- Reason: {' '.join(acc.reason.split())}")
        if asserts := assertion_lines(old, new):
            lines.append("- Assertions added:")
            lines += [f"  - `{a.replace('`', chr(39))}`" for a in asserts]
        for flag in acc.flags:
            lines.append(f"- **Review:** {flag}")
        lines.append("")
    if p.dropped:
        lines += [f"## Dropped by ATH ({len(p.dropped)})", ""]
        lines += [f"- `{d.path}` ({d.action}): {d.reason}" for d in p.dropped] + [""]
    if p.spec_questions:
        lines += ["## Spec questions", ""]
        lines += [f"- {('[' + q.clause + '] ') if q.clause else ''}{' '.join(q.question.split())}"
                  for q in p.spec_questions] + [""]
    if p.summary:
        lines += ["## Model summary", "", " ".join(p.summary.split()), ""]
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["## Reviewer checks", "",
              "- [ ] Every expected value comes from the TSD, not from what the code does today",
              "- [ ] Each **Review** flag above is intended",
              "- [ ] Card, account, mobile and Aadhaar values are `{{testdata:...}}` references",
              "- [ ] New tests call methods that exist (see the `code` block, if it was attached)", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- storage
def save_proposal(folder: str | Path, drafted: Drafted) -> str:
    """Write proposal.json (canonical), proposal.sha256, changes.patch and checklist.md. Returns the hash."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    h = drafted.proposal.hash
    (folder / "proposal.json").write_text(drafted.proposal.canonical_json(), encoding="utf-8")
    (folder / "proposal.sha256").write_text(h + "\n", encoding="utf-8")
    (folder / "changes.patch").write_text(drafted.patch, encoding="utf-8")
    (folder / "checklist.md").write_text(drafted.checklist, encoding="utf-8")
    return h


def load_proposal(folder: str | Path) -> Proposal:
    """Load a stored proposal and check it against its recorded hash. Raises ValueError when they differ."""
    folder = Path(folder)
    p = Proposal.model_validate_json((folder / "proposal.json").read_text(encoding="utf-8"))
    recorded = (folder / "proposal.sha256").read_text(encoding="utf-8").strip()
    if p.hash != recorded:
        raise ValueError(f"{folder / 'proposal.json'} does not match its recorded hash; it changed after it was made")
    return p
