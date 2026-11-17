"""T3 test authoring: what the author model returns, and the proposal ATH stores, reviews and applies.

The model's JSON is checked here field by field (`validate_output` only checks top-level keys). Each
change is parsed on its own, so one malformed change is dropped with a reason and the rest survive.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ath_core.schemas.run import utcnow

PROPOSAL_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _str_values(v: Any) -> Any:
    # Models write numbers for CSV cells (`"amount": 1250`); a cell is text.
    if isinstance(v, dict):
        return {str(k): ("" if x is None else str(x)) for k, x in v.items()}
    return v


class Edit(_Model):
    """One edit inside an `update`.

    text/json/yaml: {op: replace, find, replace}. csv/tsv: {op: add_row, values} or
    {op: update_row, key, set}. `op` defaults to replace."""
    op: Literal["replace", "add_row", "update_row"] = "replace"
    find: str | None = None
    replace: str | None = None
    values: dict[str, str] | None = None
    key: str | None = None
    set: dict[str, str] | None = None

    @field_validator("values", "set", mode="before")
    @classmethod
    def _cells_are_text(cls, v: Any) -> Any:
        return _str_values(v)

    @field_validator("key", mode="before")
    @classmethod
    def _key_text(cls, v: Any) -> Any:
        return str(v) if isinstance(v, (int, float)) else v

    def problem(self) -> str | None:
        if self.op == "replace":
            if not self.find:
                return "a replace edit needs a non-empty find"
            if self.replace is None:
                return "a replace edit needs replace"
        elif self.op == "add_row":
            if not self.values:
                return "add_row needs values"
        elif not self.key or not self.set:
            return "update_row needs key and set"
        return None


def _one_or_many(v: Any) -> Any:
    return [v] if isinstance(v, str) else v


class Change(_Model):
    action: Literal["create", "update"]
    path: str
    clauses: list[str] = Field(default_factory=list)
    plan_items: list[str] = Field(default_factory=list)  # TP- ids of the approved plan this change implements
    reason: str = ""
    content: str | None = None
    edits: list[Edit] = Field(default_factory=list)

    @field_validator("clauses", "plan_items", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _one_or_many(v)


class SpecQuestion(_Model):
    id: str = ""          # Q-n, given by ATH (never by the model), unique across clarification rounds
    clause: str = ""
    question: str
    assumption: str = ""  # what the model would assume without an answer; a tester may accept it


class Clarification(_Model):
    """A tester's answer to a spec question. It refines the clause it cites; it never edits the TSD."""
    id: str               # the question's Q- id
    clause: str = ""
    question: str
    answer: str
    accepted_assumption: bool = False  # the answer is the model's own assumption, accepted as is
    by: str = ""
    at: datetime = Field(default_factory=utcnow)


class ParsedOutput(BaseModel):
    changes: list[Change]
    malformed: list[tuple[str, str]]  # (path or "?", why) for changes that did not parse
    spec_questions: list[SpecQuestion]
    summary: str


def _questions(obj: dict[str, Any]) -> list[SpecQuestion]:
    questions = []
    for q in obj.get("spec_questions") or []:
        if isinstance(q, dict) and q.get("question"):
            questions.append(SpecQuestion(clause=str(q.get("clause") or ""), question=str(q["question"]),
                                          assumption=str(q.get("assumption") or "")))
        elif isinstance(q, str) and q.strip():
            questions.append(SpecQuestion(question=q))
    return questions


def parse_output(obj: dict[str, Any]) -> ParsedOutput:
    """The author model's JSON, change by change. Raises ValueError when the top level is unusable."""
    raw_changes = obj.get("changes")
    if not isinstance(raw_changes, list):
        raise ValueError("author output: `changes` must be a list")
    changes, malformed = [], []
    for i, rc in enumerate(raw_changes):
        try:
            changes.append(Change.model_validate(rc))
        except ValidationError as e:
            path = rc.get("path") if isinstance(rc, dict) and isinstance(rc.get("path"), str) else f"changes[{i}]"
            first = e.errors()[0]
            malformed.append((path, f"malformed change: {'.'.join(map(str, first['loc'])) or 'change'}: {first['msg']}"))
    return ParsedOutput(changes=changes, malformed=malformed, spec_questions=_questions(obj),
                        summary=str(obj.get("summary") or ""))


class AcceptedChange(_Model):
    action: Literal["create", "update"]
    path: str
    kind: Literal["code", "data"]
    format: str
    clauses: list[str]
    traced_by: Literal["id", "title"] = "id"  # "title": the TSD had no ⓘ ids
    plan_items: list[str] = Field(default_factory=list)
    reason: str = ""
    content: str | None = None
    edits: list[Edit] = Field(default_factory=list)
    base_sha256: str | None = None    # file as the model saw it (None for a create)
    result_sha256: str | None = None  # file once applied with today's catalogue values; spots a repeat apply
    flags: list[str] = Field(default_factory=list)  # review flags, for the checklist


class DroppedChange(_Model):
    action: str
    path: str
    reason: str
    clauses: list[str] = Field(default_factory=list)


class Proposal(_Model):
    """What T3 proposes. Holds catalogue references, never catalogue values or raw test-file text."""
    version: int = PROPOSAL_VERSION
    id: str
    created_at: datetime = Field(default_factory=utcnow)
    product: str
    source: str                       # "standalone" | "run:<run id>"
    mode: str = "local-fs"
    model: str = ""
    skill: str = ""
    product_skill_version: str = ""
    tsd_sha256: str = ""
    prompt_sha256: str = ""
    blocks: dict[str, int] = Field(default_factory=dict)  # characters sent per <data> block
    changes: list[AcceptedChange] = Field(default_factory=list)
    dropped: list[DroppedChange] = Field(default_factory=list)
    spec_questions: list[SpecQuestion] = Field(default_factory=list)
    summary: str = ""
    agent_call: dict[str, Any] = Field(default_factory=dict)  # audit record: hashes, never text
    proposed_count: int = 0  # changes the model proposed, before guards
    plan_id: str = ""        # the approved test plan this proposal implements ("" without a plan step)
    plan_sha256: str = ""

    def canonical_json(self) -> str:
        return _canonical(_prune(self.model_dump(mode="json"), _ADDED_KEYS))

    @property
    def hash(self) -> str:
        """The proposal hash: SHA-256 of its canonical JSON. A gate approves exactly this."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# Keys added after proposal version 1, left out of the canonical JSON while they hold their defaults: a
# proposal stored before they existed keeps the hash it was approved with.
_ADDED_KEYS: dict[str, Any] = {"plan_id": "", "plan_sha256": "",
                               "changes": {"plan_items": []}, "spec_questions": {"id": "", "assumption": ""}}


def _prune(data: dict[str, Any], added: dict[str, Any]) -> dict[str, Any]:
    for key, rule in added.items():
        if isinstance(rule, dict):
            for item in data.get(key) or []:
                _prune(item, rule)
        elif key in data and data[key] == rule:
            del data[key]
    return data


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PlanItem(_Model):
    """One scenario of a test plan: what to test, the expected result, and the test file to write it in."""
    id: str = ""          # TP-n, given by ATH
    clauses: list[str]
    traced_by: Literal["id", "title"] = "id"
    scenario: str
    expected: str = ""    # from the TSD, or from a clarification it cites
    path: str
    action: Literal["create", "update"]
    kind: str = ""        # code | data, from the testAuthoring source the path matches
    clarifications: list[str] = Field(default_factory=list)  # Q- ids the item relies on
    origin: Literal["model", "tester"] = "model"
    edited: bool = False  # a tester changed it after the model proposed it

    @field_validator("clauses", "clarifications", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _one_or_many(v)


# What the model (or a tester's plan.yaml) may set in a plan item; the rest is ATH's.
PLAN_ITEM_FIELDS = ("clauses", "scenario", "expected", "path", "action", "clarifications")


class ParsedPlan(BaseModel):
    items: list[PlanItem]
    malformed: list[tuple[str, str]]
    spec_questions: list[SpecQuestion]
    summary: str


def plan_item(raw: Any) -> PlanItem:
    """A plan item from model or tester input, without its id. Raises ValueError when malformed."""
    if not isinstance(raw, dict):
        raise ValueError("a plan item must be an object")
    unknown = sorted(set(raw) - set(PLAN_ITEM_FIELDS) - {"id"})
    if unknown:
        raise ValueError(f"unknown field(s) {unknown}")
    try:
        return PlanItem.model_validate({k: v for k, v in raw.items() if k != "id"})
    except ValidationError as e:
        first = e.errors()[0]
        raise ValueError(f"{'.'.join(map(str, first['loc'])) or 'item'}: {first['msg']}") from None


def parse_plan_output(obj: dict[str, Any]) -> ParsedPlan:
    """The planner's JSON, item by item. Raises ValueError when the top level is unusable."""
    raw_items = obj.get("plan")
    if not isinstance(raw_items, list):
        raise ValueError("plan output: `plan` must be a list")
    items, malformed = [], []
    for i, ri in enumerate(raw_items):
        try:
            items.append(plan_item(ri))
        except ValueError as e:
            path = ri.get("path") if isinstance(ri, dict) and isinstance(ri.get("path"), str) else f"plan[{i}]"
            malformed.append((path, f"malformed plan item: {e}"))
    return ParsedPlan(items=items, malformed=malformed, spec_questions=_questions(obj),
                      summary=str(obj.get("summary") or ""))


class TestPlan(_Model):
    """T3 phase 1: the scenarios to test and the questions still open. Holds masked text only.
    Approving it (by its hash) lets ATH generate code for exactly these items."""
    __test__ = False
    version: int = 1
    id: str
    parent: str = ""                  # the plan this one answers or edits
    round: int = 1                    # model rounds so far (answers add one; tester edits do not)
    created_at: datetime = Field(default_factory=utcnow)
    product: str
    source: str                       # "standalone" | "run:<run id>"
    model: str = ""
    skill: str = ""
    product_skill_version: str = ""
    tsd_sha256: str = ""
    prompt_sha256: str = ""
    clause_ids: list[str] = Field(default_factory=list)       # ⓘ ids of the TSD sent
    section_titles: list[str] = Field(default_factory=list)   # its section titles (used without ⓘ ids)
    blocks: dict[str, int] = Field(default_factory=dict)
    items: list[PlanItem] = Field(default_factory=list)
    dropped: list[DroppedChange] = Field(default_factory=list)
    spec_questions: list[SpecQuestion] = Field(default_factory=list)  # open: asked in this round
    clarifications: list[Clarification] = Field(default_factory=list)  # answered, this round and before
    summary: str = ""
    edited_by: str = ""               # set on a version made from a tester's plan.yaml edits
    agent_call: dict[str, Any] = Field(default_factory=dict)

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def proposal_hash_of(text: str) -> str:
    """Hash of a stored proposal.json, recomputed over its canonical form."""
    return Proposal.model_validate_json(text).hash


class AppliedResult(_Model):
    applied: list[str] = Field(default_factory=list)          # paths written
    already_applied: list[str] = Field(default_factory=list)  # paths already in the proposed state
    proposal_hash: str
    approved_by: str
