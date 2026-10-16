"""Run model: what the orchestrator tracks for each triggered run."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunMode(str, Enum):
    REPORT_ONLY = "report-only"
    ASSIST = "assist"
    AUTOFIX = "autofix"


class Stage(str, Enum):
    T1_IMPACT = "T1"
    T2_SELECT = "T2"
    T3_AUTHOR = "T3"
    T4_ENV = "T4"
    T5_EXECUTE = "T5"
    T6_NORMALIZE = "T6"
    T7_TRIAGE = "T7"
    T8_VERIFY = "T8"
    T9_REPORT = "T9"
    T10_REMEDIATE = "T10"
    T11_VALIDATE = "T11"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    WAITING_GATE = "waiting-gate"
    FAILED = "failed"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_GATE = "waiting-gate"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunContext(BaseModel):
    """Inputs of a run. Fixed once the run starts."""
    run_id: str
    product: str
    mode: RunMode = RunMode.REPORT_ONLY
    mr: str | None = None
    jira: str | None = None
    commit: str
    merge_base: str
    env: str
    changed_paths: list[str] = Field(default_factory=list)
    triggered_by: str  # SSO user id
    trigger_source: str = "cli"  # cli | web | adlc | schedule


class StageResult(BaseModel):
    stage: Stage
    status: StageStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunManifest(BaseModel):
    """Audit record of a run: pinned versions, inputs, stage outcomes, cost."""
    context: RunContext
    core_version: str
    package_version: str
    profile_hash: str
    status: RunStatus = RunStatus.QUEUED
    stages: dict[Stage, StageResult] = Field(default_factory=dict)
    log_sources_used: list[str] = Field(default_factory=list)
    est_tokens: int = 0
    actual_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow)
