"""T3 authoring engine, in two phases. Each phase is one model call whose output ATH checks.

Used by `ainxt-ath author` (standalone, no run) and by stage T3 of a run.

Phase 1, plan (skill t03-plan): the TSD becomes a test plan, one item per scenario with its expected
result and target file, plus spec questions for clauses the TSD leaves unclear. A tester answers the
questions (`Clarification`s: they refine a clause, they never edit the TSD) and plans again, edits the
plan if needed, and approves it.

Phase 2, generate (skill t03-author), for the approved plan only:
  1. gather context: one masked, size-capped <data> block per input, selected deterministically
  2. one call to skill t03-author (a single author; a human reviews the result)
  3. parse its JSON change by change
  4. guards: a change that breaks a rule, or implements no approved plan item, is dropped with the
     reason, never written
  5. review flags: weakened tests, new imports, risky API calls, expectations resting on a tester's
     answer or plan edit (they do not drop the change)
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
from typing import Any, Callable, Protocol

import yaml

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
    AcceptedChange, Change, Clarification, DroppedChange, PlanItem, Proposal, TestPlan, parse_output,
    parse_plan_output, plan_item,
)
from ath_core.schemas.profile import TestProfile, TestSource
from ath_core.schemas.run import utcnow

SKILL = "t03-author"
PLAN_SKILL = "t03-plan"
PLACEHOLDER = "(not attached)"
BLOCKS = ("tsd", "tests", "tree", "conventions", "testdata", "product", "code", "templates", "diff", "impact",
          "outdated", "clarifications", "plan")
ACCEPT = ("accept", "a")  # an answer that takes the model's own assumption
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
    clarifications: list[Clarification] | None = None  # answered spec questions, masked
    plan: TestPlan | None = None  # generate phase: the approved plan; its files are sent first


def authoring_masker(profile: TestProfile) -> Masker:
    """Every category, whatever the profile's `logs.masking` says: test files are not logs, and a
    category switched off for logs must not let raw test data reach the model. `testAuthoring.notPii`
    fields are exempt as the Masker docstring says."""
    ta = profile.test_authoring
    return Masker(categories=list(CATEGORIES), correlation_keys=profile.correlation_keys(),
                  not_pii_keys=list(ta.not_pii) if ta else [])


def _not_pii_cells(text: str, fmt: str, names: list[str]) -> set[str]:
    """The cell values in a CSV/TSV's `testAuthoring.notPii` columns (header names, any case).
    Empty when nothing is declared or the text does not parse, so masking stays on."""
    if not names or fmt not in ("csv", "tsv"):
        return set()
    try:
        layout = read_csv(text, fmt)
    except (GuardError, ValueError):
        return set()
    wanted = {n.lower() for n in names}
    cols = [i for i, h in enumerate(layout.header) if h.strip().lower() in wanted]
    return {v for _, _, row in layout.data_records() for i in cols if i < len(row) for v in (row[i], row[i].strip())}


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
        # for what the model writes: no correlation-key exemption, but declared not-PII fields are exempt
        self._written_masker = Masker(categories=list(CATEGORIES), not_pii_keys=list(self.ta.not_pii))
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
                exempt = _not_pii_cells(tf.text, source.edit_format, self.ta.not_pii)
                masked, spans = self.masker.mask_spans(tf.text, exempt_values=exempt)
                self._views[rel] = FileView(rel, source.kind, source.edit_format, tf, masked, spans,
                                            sha256_bytes(data), source.key_column)
        return self._views[rel]

    def check_written(self, text: str, exempt_values: set[str] | frozenset[str] = frozenset()) -> None:
        """Text the model wrote: no mask tokens, only known catalogue references, nothing Masker would mask.
        `exempt_values`: whole values declared not PII (cells of a `notPii` column)."""
        if m := MASK_TOKEN_RE.search(text):
            raise GuardError(f"model wrote the mask token {m.group(0)}; mask tokens are redacted values, "
                             f"never literals: use a testData reference")
        if bad := self.catalogue.unknown_refs(text):
            raise GuardError(f"unknown test-data reference(s) {bad}")
        masked, spans = self._written_masker.mask_spans(text, exempt_values=exempt_values)
        if spans:
            found = []
            for s in spans:
                cat = s.category.split("+")[0]
                found.append(f"{_WHAT.get(cat, cat)} ({masked[s.masked_start:s.masked_end]})")
            raise GuardError(f"model wrote a value that looks like {', '.join(dict.fromkeys(found))}; "
                             f"use a testData reference or another value")

    def _written_parts(self, change: Change | AcceptedChange, source: TestSource) -> list[tuple[str, set[str]]]:
        """Every piece of text the model wrote, with the whole values in it that are declared not PII."""
        names = {n.lower() for n in self.ta.not_pii}
        fmt = source.edit_format

        def cell(column: str | None, value: str) -> tuple[str, set[str]]:
            return value, ({value, value.strip()} if column and column.strip().lower() in names else set())

        content = change.content or ""
        parts = [(content, _not_pii_cells(content, fmt, self.ta.not_pii))]
        for e in change.edits:
            # `key` too: it is stored in the proposal, so it must not carry a raw value either
            parts += [(e.replace or "", set()), cell(source.key_column, e.key or "")]
            parts += [cell(c, v) for c, v in (e.values or {}).items()]
            parts += [cell(c, v) for c, v in (e.set or {}).items()]
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
        parts = self._written_parts(change, source)
        if sum(len(p) for p, _ in parts) > self.ta.limits.max_change_chars:
            raise GuardError(f"change is over the limit of {self.ta.limits.max_change_chars} characters")
        for text, exempt in parts:
            self.check_written(text, exempt)
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

    def check_plan_item(self, item: PlanItem, editable: set[str] | None, answered: set[str]) -> TestSource:
        """The checks a plan item must pass, or GuardError: the same path and create/update rules as a
        change, and every clarification it cites was answered. `editable` None skips the sent-whole
        check (tester edits: generation sends plan files first, and `realize` checks again)."""
        target, source = self.check_path(item.path)
        if not item.scenario.strip():
            raise GuardError("a plan item needs a scenario")
        if item.action == "create" and (target.exists() or target.is_symlink()):
            raise GuardError("create is only for a file that does not exist; use update")
        if item.action == "update":
            if not target.is_file():
                raise GuardError("update is only for a file that exists; use create")
            if editable is not None and item.path not in editable:
                raise GuardError("the file was not sent whole to the model, so it cannot be updated")
        if bad := [q for q in item.clarifications if q not in answered]:
            raise GuardError(f"cites clarification(s) {bad} that were not answered")
        return source


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
    def __init__(self, profile: TestProfile, agent: AuthorAgent | None = None, planner: AuthorAgent | None = None):
        """`agent` runs skill t03-author (generate), `planner` skill t03-plan (plan)."""
        if profile.test_authoring is None:
            raise ValueError(f"profile {profile.product} has no testAuthoring section")
        self.profile = profile
        self.ta = profile.test_authoring
        self.agent = agent
        self.planner = planner

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
        # The approved plan's files go first, so an update it plans finds its file sent whole.
        planned = {i.path for i in inputs.plan.items} if inputs.plan else set()
        order = sorted(rank([(v.path, v.masked) for v in views], wanted), key=lambda i: views[i].path not in planned)
        for i in order:
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
        if inputs.clarifications:
            blocks["clarifications"] = _cap(mask(clarifications_text(inputs.clarifications)),
                                            lim.clarifications_chars)
        if inputs.plan:
            blocks["plan"] = _cap(mask(plan_text(inputs.plan)), lim.plan_chars)
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

    @staticmethod
    def _call(agent: AuthorAgent, blocks: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            out = agent.run(blocks)
        finally:
            call = dict(getattr(agent, "last_call", {}) or {})
        return out, call

    # -------------------------------------------------------------------------------------- plan
    def plan(self, inputs: AuthorInputs, source: str, plan_id: str | None = None,
             parent: TestPlan | None = None) -> "DraftedPlan":
        """Phase 1: gather, call the planner once, check every plan item. `parent` is the plan whose
        questions `inputs.clarifications` answer. Raises ValueError for unusable input and the agent's
        own error (AgentError) when the call fails."""
        if self.planner is None:
            raise ValueError("no planning agent configured")
        ws = Workspace(self.profile, inputs.tests_dir)
        g = self.gather(ws, inputs)
        out, call = self._call(self.planner, g.blocks)
        parsed = parse_plan_output(out)
        m = ws.masker.mask
        answered = {c.id for c in inputs.clarifications or []}
        # Q ids continue across rounds, so an answer always names one question.
        seen = answered | {q.id for q in (parent.spec_questions if parent else [])}
        n = _max_num(seen, "Q")
        for q in parsed.spec_questions:
            n += 1
            q.id, q.clause, q.question, q.assumption = f"Q-{n}", m(q.clause), m(q.question), m(q.assumption)
        ids = tsd_clause_ids(g.tsd_sent)
        sections = [s.title for s in tsd_sections(g.tsd_sent) if s.title]
        titles = {t.casefold(): t for t in sections}
        items: list[PlanItem] = []
        dropped = [DroppedChange(action="?", path=m(p), reason=m(why)) for p, why in parsed.malformed]
        for it in parsed.items:
            it.scenario, it.expected, it.clauses = m(it.scenario), m(it.expected), [m(c) for c in it.clauses]
            try:
                if len(items) >= self.ta.limits.plan_items:
                    raise GuardError(f"over the limit of {self.ta.limits.plan_items} plan items")
                it.traced_by = _trace(it.clauses, ids, titles)
                it.kind = ws.check_plan_item(it, g.editable, answered).kind
            except GuardError as e:
                dropped.append(DroppedChange(action=it.action, path=m(it.path), reason=m(str(e)), clauses=it.clauses))
                continue
            it.id = f"TP-{len(items) + 1}"
            items.append(it)
        plan = TestPlan(
            id=plan_id or f"plan-{uuid.uuid4().hex[:10]}", parent=parent.id if parent else "",
            round=parent.round + 1 if parent else 1, product=self.profile.product, source=source,
            model=getattr(self.planner, "model", ""), skill=PLAN_SKILL, product_skill_version=g.product_skill_version,
            tsd_sha256=sha256_bytes(g.tsd_sent.encode("utf-8")), prompt_sha256=str(call.get("prompt_sha256", "")),
            clause_ids=sorted(ids), section_titles=sections, blocks={k: len(v) for k, v in g.blocks.items()},
            items=items, dropped=dropped, spec_questions=parsed.spec_questions,
            clarifications=list(inputs.clarifications or []), summary=m(parsed.summary), agent_call=call)
        return DraftedPlan(plan, render_plan_md(plan, g.notes))

    # ---------------------------------------------------------------------------------- proposal
    def propose(self, inputs: AuthorInputs, source: str, proposal_id: str | None = None) -> "Drafted":
        """Phase 2: gather, call the model once, check every change. With `inputs.plan`, only changes that
        implement its items are kept. Raises ValueError for unusable input and the agent's own error
        (AgentError) when the call fails."""
        if self.agent is None:
            raise ValueError("no author agent configured")
        ws = Workspace(self.profile, inputs.tests_dir)
        g = self.gather(ws, inputs)
        out, call = self._call(self.agent, g.blocks)
        parsed = parse_output(out)
        # Free text the model wrote is stored and shown: mask it like everything else.
        m = ws.masker.mask
        parsed.summary = m(parsed.summary)
        for q in parsed.spec_questions:
            q.clause, q.question, q.assumption = m(q.clause), m(q.question), m(q.assumption)
        for ch in parsed.changes:
            ch.reason, ch.clauses, ch.plan_items = m(ch.reason), [m(c) for c in ch.clauses], [m(p) for p in ch.plan_items]
        ids = tsd_clause_ids(g.tsd_sent)
        titles = {s.title.casefold(): s.title for s in tsd_sections(g.tsd_sent) if s.title}
        plan = inputs.plan
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
                items = _plan_items(ch, plan) if plan else []
                r = ws.realize(ch, editable=g.editable)
            except GuardError as e:
                dropped.append(DroppedChange(action=ch.action, path=m(ch.path), reason=m(str(e)), clauses=ch.clauses))
                continue
            acc = AcceptedChange(
                action=ch.action, path=ch.path, kind=r.source.kind, format=r.source.edit_format,
                clauses=ch.clauses, traced_by=traced_by, plan_items=ch.plan_items, reason=ch.reason,
                content=ch.content, edits=ch.edits, base_sha256=r.base_sha256, result_sha256=sha256_bytes(r.new_bytes),
                flags=review_flags(r.masked_old, r.masked_new, r.source.kind) + _plan_flags(items))
            accepted.append(acc)
            rendered.append((acc, r.masked_old, r.masked_new))
        proposal = Proposal(
            id=proposal_id or f"author-{uuid.uuid4().hex[:10]}", product=self.profile.product, source=source,
            mode=self.ta.mode, model=getattr(self.agent, "model", ""), skill=SKILL,
            product_skill_version=g.product_skill_version,
            tsd_sha256=sha256_bytes(g.tsd_sent.encode("utf-8")), prompt_sha256=str(call.get("prompt_sha256", "")),
            blocks={k: len(v) for k, v in g.blocks.items()}, changes=accepted, dropped=dropped,
            spec_questions=parsed.spec_questions, summary=parsed.summary, agent_call=call,
            proposed_count=len(parsed.changes) + len(parsed.malformed),
            plan_id=plan.id if plan else "", plan_sha256=plan.hash if plan else "")
        return Drafted(proposal, render_patch(rendered), render_checklist(proposal, rendered, g.notes, plan))


def _max_num(ids: set[str], prefix: str) -> int:
    nums = [int(i.split("-", 1)[1]) for i in ids if re.fullmatch(rf"{prefix}-\d+", i)]
    return max(nums, default=0)


def _plan_items(ch: Change, plan: TestPlan) -> list[PlanItem]:
    """The approved plan items a change implements, or GuardError."""
    by_id = {i.id: i for i in plan.items}
    if not ch.plan_items:
        raise GuardError("cites no plan item; every change must implement an approved plan item")
    if bad := [p for p in ch.plan_items if p not in by_id]:
        raise GuardError(f"plan item(s) {bad} are not in the approved plan {plan.id}")
    items = [by_id[p] for p in ch.plan_items]
    if other := [i.id for i in items if i.path != ch.path]:
        raise GuardError(f"plan item(s) {other} name another file, not this one")
    return items


def _plan_flags(items: list[PlanItem]) -> list[str]:
    """Review flags for expectations that do not come from the signed-off TSD alone."""
    flags = []
    if qs := sorted({q for i in items for q in i.clarifications}):
        flags.append(f"expected values rely on tester answer(s) {', '.join(qs)}, not on the signed-off TSD; "
                     f"confirm them with the TSD owner")
    if added := [i.id for i in items if i.origin == "tester"]:
        flags.append(f"plan item(s) {', '.join(added)} were added by a tester")
    if edited := [i.id for i in items if i.origin == "model" and i.edited]:
        flags.append(f"plan item(s) {', '.join(edited)} were edited by a tester")
    return flags


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


@dataclass
class DraftedPlan:
    plan: TestPlan
    markdown: str


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


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _question_line(q) -> str:
    head = f"{q.id} " if q.id else ""
    line = f"- {head}{('[' + q.clause + '] ') if q.clause else ''}{_one_line(q.question)}"
    return line + (f"\n  - Assumption if unanswered: {_one_line(q.assumption)}" if q.assumption else "")


def _clarification_lines(clarifications: list[Clarification]) -> list[str]:
    lines = ["Tester answers to spec questions. They are not in the signed-off TSD: send them to the TSD "
             "owner so the TSD can be updated.", ""]
    for c in clarifications:
        how = " (the model's assumption, accepted)" if c.accepted_assumption else ""
        lines.append(f"- {c.id} {('[' + c.clause + '] ') if c.clause else ''}{_one_line(c.question)}")
        lines.append(f"  - Answer{how}: {_one_line(c.answer)} — {c.by or 'unknown'}, {c.at:%Y-%m-%d}")
    return lines + [""]


def clarifications_text(clarifications: list[Clarification]) -> str:
    """The `clarifications` block for the model."""
    return "\n".join(f"- {c.id} [{c.clause or 'no clause'}] question: {_one_line(c.question)}\n"
                     f"  answer: {_one_line(c.answer)}" for c in clarifications)


def plan_text(plan: TestPlan) -> str:
    """The `plan` block for the author: the approved items only."""
    out = []
    for i in plan.items:
        out.append(f"- {i.id} [{', '.join(i.clauses)}] {i.action} {i.path}\n  scenario: {_one_line(i.scenario)}\n"
                   f"  expected: {_one_line(i.expected) or '(see the clauses)'}"
                   + (f"\n  clarifications: {', '.join(i.clarifications)}" if i.clarifications else ""))
    return "\n".join(out) or "(the approved plan has no items)"


def render_plan_md(p: TestPlan, notes: list[str] | None = None) -> str:
    lines = [f"# Test plan: {p.id}", "",
             f"Product `{p.product}` · source `{p.source}` · model `{p.model or '(none)'}` · skill `{p.skill}` · "
             f"round {p.round}" + (f" · follows `{p.parent}`" if p.parent else "")
             + (f" · edited by {p.edited_by}" if p.edited_by else ""), ""]
    lines += [f"## Scenarios ({len(p.items)})", ""]
    if not p.items:
        lines += ["None.", ""]
    for i in p.items:
        who = " · added by a tester" if i.origin == "tester" else " · edited by a tester" if i.edited else ""
        lines.append(f"### {i.id} {i.action} `{i.path}` ({i.kind or '?'}){who}")
        trace = ", ".join(i.clauses) + (" (traced by section title only)" if i.traced_by == "title" else "")
        lines.append(f"- Clauses: {trace}")
        lines.append(f"- Scenario: {_one_line(i.scenario)}")
        lines.append(f"- Expected: {_one_line(i.expected) or '(not given)'}")
        if i.clarifications:
            lines.append(f"- **Relies on tester answer(s):** {', '.join(i.clarifications)}")
        lines.append("")
    if p.spec_questions:
        lines += [f"## Open spec questions ({len(p.spec_questions)})", "",
                  "Clauses with an open question are not planned. Answer them in answers.yaml "
                  "(`accept` takes the assumption).", ""]
        lines += [_question_line(q) for q in p.spec_questions] + [""]
    if p.clarifications:
        lines += [f"## Clarifications ({len(p.clarifications)})", ""] + _clarification_lines(p.clarifications)
    if p.dropped:
        lines += [f"## Dropped by ATH ({len(p.dropped)})", ""]
        lines += [f"- `{d.path}` ({d.action}): {d.reason}" for d in p.dropped] + [""]
    if p.summary:
        lines += ["## Model summary", "", _one_line(p.summary), ""]
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["## Reviewer checks", "",
              "- [ ] Every TSD clause in scope has a scenario, or an open question",
              "- [ ] Each expected result matches the TSD (or a clarification it cites)",
              "- [ ] Each target file is the right place for the scenario", ""]
    return "\n".join(lines)


def _coverage_lines(plan: TestPlan, p: Proposal) -> list[str]:
    done = {pi for c in p.changes for pi in c.plan_items}
    dropped_paths = {d.path for d in p.dropped}
    lines = [f"## Plan coverage ({plan.id})", "", "| Item | Scenario | File | Status |", "| --- | --- | --- | --- |"]
    for i in plan.items:
        status = ("implemented" if i.id in done else "dropped by ATH" if i.path in dropped_paths
                  else "**not implemented**")
        lines.append(f"| {i.id} | {_one_line(i.scenario).replace('|', '/')} | `{i.path}` | {status} |")
    return lines + [""]


def render_checklist(p: Proposal, rendered: list[tuple[AcceptedChange, str, str]], notes: list[str],
                     plan: TestPlan | None = None) -> str:
    lines = [f"# Test authoring checklist: {p.id}", "",
             f"Product `{p.product}` · source `{p.source}` · model `{p.model or '(none)'}` · skill `{p.skill}`"
             + (f" · product skill v{p.product_skill_version}" if p.product_skill_version else ""), "",
             "Values from the test-data catalogue appear as `{{testdata:...}}` references; existing test data "
             "is shown masked.", ""]
    if plan:
        lines += _coverage_lines(plan, p)
    lines += [f"## Changes ({len(p.changes)})", ""]
    if not p.changes:
        lines += ["None.", ""]
    for acc, old, new in rendered:
        lines.append(f"### {acc.action} `{acc.path}` ({acc.kind}, {acc.format})")
        trace = ", ".join(acc.clauses) + (" (traced by section title only)" if acc.traced_by == "title" else "")
        lines.append(f"- Clauses: {trace}")
        if acc.plan_items:
            lines.append(f"- Plan items: {', '.join(acc.plan_items)}")
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
        lines += [_question_line(q) for q in p.spec_questions] + [""]
    if plan and plan.clarifications:
        lines += [f"## Clarifications ({len(plan.clarifications)})", ""] + _clarification_lines(plan.clarifications)
    if p.summary:
        lines += ["## Model summary", "", " ".join(p.summary.split()), ""]
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["## Reviewer checks", "",
              "- [ ] Every expected value comes from the TSD, not from what the code does today",
              "- [ ] Each **Review** flag above is intended",
              "- [ ] Card, account, mobile and Aadhaar values are `{{testdata:...}}` references",
              "- [ ] New tests call methods that exist (see the `code` block, if it was attached)"]
    if plan and plan.clarifications:
        lines.append("- [ ] Each expectation resting on a clarification is confirmed by the TSD owner")
    return "\n".join(lines + [""])


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


# ------------------------------------------------------------------------------- plan storage
def plan_yaml(p: TestPlan, next_step: str = "") -> str:
    """The editable plan: a tester removes, edits or adds items, then generates from it."""
    head = [f"# Test plan {p.id} for {p.product}. Review plan.md, edit here if needed, then generate"
            + (f":\n#   {next_step}" if next_step else "."),
            "# Remove an item to leave it out; edit its scenario or expected result; add an item without an",
            "# `id` to add one. Every item cites TSD clauses; `update` needs an existing test file.",
            "# Fields: action (create|update), path, clauses, scenario, expected, clarifications (Q- ids)."]
    items = [{"id": i.id, "action": i.action, "path": i.path, "clauses": i.clauses, "scenario": i.scenario,
              "expected": i.expected, **({"clarifications": i.clarifications} if i.clarifications else {})}
             for i in p.items]
    body = yaml.safe_dump({"plan": p.id, "plan_sha256": p.hash, "items": items}, sort_keys=False,
                          allow_unicode=True, width=100)
    return "\n".join(head) + "\n" + body


def answer_sheet(p: TestPlan, next_step: str = "", answers: dict[str, str] | None = None) -> str:
    """answers.yaml for the plan's open questions; `answers` pre-fills it (a record of typed answers)."""
    head = [f"# Spec questions of test plan {p.id}. For each question, write `answer`:",
            "#   your answer as text, `accept` to take the model's assumption, or leave it empty to skip.",
            "# Answers refine the clause they cite; they do not change the TSD. Then plan again"
            + (f":\n#   {next_step}" if next_step else ".")]
    rows = [{"id": q.id, "clause": q.clause, "question": q.question, "assumption": q.assumption,
             "answer": (answers or {}).get(q.id, "")} for q in p.spec_questions]
    return "\n".join(head) + "\n" + yaml.safe_dump({"plan": p.id, "plan_sha256": p.hash, "answers": rows},
                                                   sort_keys=False, allow_unicode=True, width=100)


def save_plan(folder: str | Path, drafted: DraftedPlan, next_steps: tuple[str, str] = ("", "")) -> str:
    """Write plan.json (canonical), plan.sha256, plan.md, plan.yaml and, with open questions,
    answers.yaml. `next_steps`: the commands to put in plan.yaml and answers.yaml. Returns the hash."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = drafted.plan
    (folder / "plan.json").write_text(p.canonical_json(), encoding="utf-8")
    (folder / "plan.sha256").write_text(p.hash + "\n", encoding="utf-8")
    (folder / "plan.md").write_text(drafted.markdown, encoding="utf-8")
    (folder / "plan.yaml").write_text(plan_yaml(p, next_steps[0]), encoding="utf-8")
    if p.spec_questions:
        (folder / "answers.yaml").write_text(answer_sheet(p, next_steps[1]), encoding="utf-8")
    return p.hash


def load_plan(folder: str | Path) -> TestPlan:
    """Load a stored plan and check it against its recorded hash. Raises ValueError when they differ."""
    folder = Path(folder)
    p = TestPlan.model_validate_json((folder / "plan.json").read_text(encoding="utf-8"))
    recorded = (folder / "plan.sha256").read_text(encoding="utf-8").strip()
    if p.hash != recorded:
        raise ValueError(f"{folder / 'plan.json'} does not match its recorded hash; it changed after it was made")
    return p


def answers_from(entries: Any, plan: TestPlan, user: str, mask: Callable[[str], str]) -> list[Clarification]:
    """Clarifications from `[{id, answer}]` for the plan's open questions, masked. Blank answers are
    skipped (the question stays open). Raises ValueError for an unknown id or a bad entry."""
    if not isinstance(entries, list):
        raise ValueError("answers must be a list of {id, answer}")
    by_id = {q.id: q for q in plan.spec_questions}
    out: list[Clarification] = []
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            raise ValueError("each answer needs an `id` (Q-n) and an `answer`")
        q = by_id.get(e["id"])
        if q is None:
            raise ValueError(f"{e['id']} is not an open question of plan {plan.id} "
                             f"(open: {', '.join(by_id) or 'none'})")
        if any(c.id == q.id for c in out):
            raise ValueError(f"{q.id} is answered twice")
        text = str(e.get("answer") if e.get("answer") is not None else "").strip()
        if not text:
            continue
        accepted = text.lower() in ACCEPT
        if accepted:
            if not q.assumption:
                raise ValueError(f"{q.id} has no assumption to accept; write an answer")
            text = q.assumption
        out.append(Clarification(id=q.id, clause=q.clause, question=q.question, answer=mask(text),
                                 accepted_assumption=accepted, by=user))
    return out


def load_answers(text: str, plan: TestPlan, user: str, mask: Callable[[str], str]) -> list[Clarification]:
    """Clarifications from an answers.yaml. Raises ValueError for a sheet that cannot be used."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"answers file is not valid YAML: {e}") from None
    if not isinstance(data, dict) or "answers" not in data:
        raise ValueError("answers file needs an `answers:` list")
    return answers_from(data["answers"], plan, user, mask)


def apply_plan_edits(plan: TestPlan, text: str, ws: Workspace, user: str, new_id: str) -> TestPlan | None:
    """A new plan version from a tester's plan.yaml, or None when it changes nothing. Items keep their
    id, are marked `edited` when changed and `origin: tester` when added; every item passes the same
    checks as a planned one. No model call. Raises ValueError for an edit that cannot be used."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"plan file is not valid YAML: {e}") from None
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("plan file needs an `items:` list")
    by_id = {i.id: i for i in plan.items}
    ids, titles = set(plan.clause_ids), {t.casefold(): t for t in plan.section_titles}
    answered = {c.id for c in plan.clarifications}
    n = _max_num(set(by_id), "TP")
    items: list[PlanItem] = []
    for k, entry in enumerate(raw, 1):
        where = f"plan item {entry.get('id') or '#' + str(k)}" if isinstance(entry, dict) else f"plan item #{k}"
        try:
            old_id = entry.get("id") if isinstance(entry, dict) else None
            orig = by_id.get(str(old_id)) if old_id else None
            if old_id and orig is None:
                raise ValueError(f"unknown id {old_id}; leave `id` out to add an item")
            item = plan_item(entry)
            item.scenario, item.expected = ws.masker.mask(item.scenario), ws.masker.mask(item.expected)
            if orig is not None:
                if any(i.id == orig.id for i in items):
                    raise ValueError("listed twice")
                same = all(getattr(item, f) == getattr(orig, f) for f in
                           ("action", "path", "clauses", "scenario", "expected", "clarifications"))
                if same:
                    items.append(orig)
                    continue
                item.id, item.origin, item.edited = orig.id, orig.origin, True
            else:
                n += 1
                item.id, item.origin = f"TP-{n}", "tester"
            if len(items) >= ws.ta.limits.plan_items:
                raise GuardError(f"over the limit of {ws.ta.limits.plan_items} plan items")
            item.traced_by = _trace(item.clauses, ids, titles)
            item.kind = ws.check_plan_item(item, None, answered).kind
        except (ValueError, GuardError) as e:
            raise ValueError(f"{where}: {e}") from None
        items.append(item)
    if items == plan.items:
        return None
    return plan.model_copy(update={"id": new_id, "parent": plan.id, "created_at": utcnow(), "items": items,
                                   "edited_by": user, "agent_call": {}, "prompt_sha256": ""}, deep=True)
