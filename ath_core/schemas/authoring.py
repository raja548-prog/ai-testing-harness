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


class Change(_Model):
    action: Literal["create", "update"]
    path: str
    clauses: list[str] = Field(default_factory=list)
    reason: str = ""
    content: str | None = None
    edits: list[Edit] = Field(default_factory=list)

    @field_validator("clauses", mode="before")
    @classmethod
    def _clause_list(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v


class SpecQuestion(_Model):
    clause: str = ""
    question: str


class ParsedOutput(BaseModel):
    changes: list[Change]
    malformed: list[tuple[str, str]]  # (path or "?", why) for changes that did not parse
    spec_questions: list[SpecQuestion]
    summary: str


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
    questions = []
    for q in obj.get("spec_questions") or []:
        if isinstance(q, dict) and q.get("question"):
            questions.append(SpecQuestion(clause=str(q.get("clause", "")), question=str(q["question"])))
        elif isinstance(q, str) and q.strip():
            questions.append(SpecQuestion(question=q))
    return ParsedOutput(changes=changes, malformed=malformed, spec_questions=questions,
                        summary=str(obj.get("summary") or ""))


class AcceptedChange(_Model):
    action: Literal["create", "update"]
    path: str
    kind: Literal["code", "data"]
    format: str
    clauses: list[str]
    traced_by: Literal["id", "title"] = "id"  # "title": the TSD had no ⓘ ids
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

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def hash(self) -> str:
        """The proposal hash: SHA-256 of its canonical JSON. A gate approves exactly this."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def proposal_hash_of(text: str) -> str:
    """Hash of a stored proposal.json, recomputed over its canonical form."""
    return Proposal.model_validate_json(text).hash


class AppliedResult(_Model):
    applied: list[str] = Field(default_factory=list)          # paths written
    already_applied: list[str] = Field(default_factory=list)  # paths already in the proposed state
    proposal_hash: str
    approved_by: str
