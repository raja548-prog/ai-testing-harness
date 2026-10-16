"""Deterministic triage pre-checks (no LLM). Run before any agent sees a failure."""
from __future__ import annotations

from datetime import date
from typing import Callable

from ath_core.schemas.atrf import ATRFCase, ATRFRun, Triage, TriageCategory
from ath_core.schemas.profile import KnownFailure


def apply_prechecks(
    run: ATRFRun,
    baseline: ATRFRun | None,
    known_failures: list[KnownFailure],
    quarantined: set[str],
    rerun_passes: Callable[[ATRFCase], bool] | None = None,
    today: date | None = None,
) -> list[ATRFCase]:
    """Label failures that need no agent. Returns the failures still untriaged.

    Order matters: known failure > quarantined > pre-existing on baseline > flaky on retry.
    """
    today = today or date.today()
    active_known = {k.case_id: k for k in known_failures if date.fromisoformat(k.expires) >= today}
    base = baseline.by_id() if baseline else {}
    remaining: list[ATRFCase] = []
    for case in run.failing():
        if case.id in active_known:
            k = active_known[case.id]
            case.triage = Triage(category=TriageCategory.KNOWN_FAILURE, decided_by="precheck",
                                 reason=f"accepted on target branch ({k.ticket}, until {k.expires})")
        elif case.id in quarantined:
            case.triage = Triage(category=TriageCategory.QUARANTINED, decided_by="precheck",
                                 reason="test is on the quarantine list")
        elif case.id in base and base[case.id].is_failing:
            case.triage = Triage(category=TriageCategory.PRE_EXISTING, decided_by="precheck",
                                 reason=f"also fails on merge-base {baseline.commit}")
        elif rerun_passes is not None and rerun_passes(case):
            case.triage = Triage(category=TriageCategory.FLAKY, decided_by="precheck",
                                 reason="passed on retry; proposed for quarantine")
        else:
            remaining.append(case)
    return remaining


def mark_flaky(cases: list[ATRFCase], passed_on_retry: set[str]) -> list[ATRFCase]:
    """Label cases that passed on a suite rerun as flaky. Returns those still untriaged."""
    remaining = []
    for case in cases:
        if case.id in passed_on_retry:
            case.triage = Triage(category=TriageCategory.FLAKY, decided_by="precheck",
                                 reason="passed on retry; proposed for quarantine")
        else:
            remaining.append(case)
    return remaining


def mark_env_blocked(run: ATRFRun, reason: str) -> None:
    for case in run.failing():
        case.triage = Triage(category=TriageCategory.ENVIRONMENT, decided_by="precheck", reason=reason)
