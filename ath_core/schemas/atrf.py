"""ATRF: AiNxt Test Result Format. One normalized result format for every product."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

ATRF_VERSION = "1.0"


class CaseStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TriageCategory(str, Enum):
    CODE_REGRESSION = "code-regression"
    TEST_OUTDATED = "test-outdated"
    TEST_DATA = "test-data"
    ENVIRONMENT = "environment"
    PRE_EXISTING = "pre-existing"
    KNOWN_FAILURE = "known-failure"
    FLAKY = "flaky"
    QUARANTINED = "quarantined"
    UNKNOWN = "unknown"


BLOCKING_CATEGORIES = {TriageCategory.CODE_REGRESSION, TriageCategory.UNKNOWN}


class ATRFFailure(BaseModel):
    type: str = "assertion"
    message: str = ""
    trace: str = ""


class Triage(BaseModel):
    category: TriageCategory
    confidence: float = Field(1.0, ge=0, le=1)
    decided_by: str  # "precheck" | "vote" | "human"
    reason: str = ""
    votes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)  # verified quotes from the inputs, one per agreeing vote
    notes: list[str] = Field(default_factory=list)     # why votes were discarded or came back unknown


class ATRFCase(BaseModel):
    id: str
    name: str
    suite: str
    status: CaseStatus
    duration_ms: int = 0
    tags: list[str] = Field(default_factory=list)
    failure: ATRFFailure | None = None
    correlation: dict[str, str] = Field(default_factory=dict)
    log_refs: list[str] = Field(default_factory=list)
    triage: Triage | None = None

    @property
    def is_failing(self) -> bool:
        return self.status in (CaseStatus.FAILED, CaseStatus.ERROR)


class ATRFSummary(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0


class ATRFRun(BaseModel):
    atrf: str = ATRF_VERSION
    run_id: str
    product: str
    suite: str
    env: str
    commit: str
    baseline_commit: str | None = None
    started_at: datetime | None = None
    duration_sec: int = 0
    incomplete: bool = False
    cases: list[ATRFCase] = Field(default_factory=list)

    @property
    def summary(self) -> ATRFSummary:
        s = ATRFSummary(total=len(self.cases))
        for c in self.cases:
            setattr(s, c.status.value, getattr(s, c.status.value) + 1)
        return s

    def failing(self) -> list[ATRFCase]:
        return [c for c in self.cases if c.is_failing]

    def by_id(self) -> dict[str, ATRFCase]:
        return {c.id: c for c in self.cases}
