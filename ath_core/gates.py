"""HITL gate rules: who may approve or reject a gate, with separation of duties.

Approve (T3 test changes, T10 redeploy of a fix, T11 verdict):
  - the run must be waiting at that gate
  - a named person decides; service identities and the CLI's default user cannot
  - not the person who triggered the run: ATH acted on their behalf, so they are the author
  - a member of one of the product's owning groups (profile `owners`)
  - each gate is approved once
Reject: the run must be waiting at that gate, and the decider is a named person who either
triggered the run or is in an owning group. Stopping is always the safe direction.

Groups come from SSO in the platform; the CLI's --groups and the API's X-ATH-Groups header stand in.
TODO (platform): step-up SSO (a fresh login) for T10 redeploys and every autofix gate.
"""
from __future__ import annotations

from ath_core.schemas.run import RunManifest, RunStatus, Stage, StageStatus

NOT_A_PERSON = {"", "local-user", "anonymous"}
SERVICE_PREFIXES = ("ath-", "svc-", "service-")


def _waiting(m: RunManifest, stage: Stage) -> str | None:
    res = m.stages.get(stage)
    if m.status != RunStatus.WAITING_GATE or res is None or res.status != StageStatus.WAITING_GATE:
        return (f"run {m.context.run_id} is not waiting at gate {stage.value} "
                f"(run {m.status.value}, {stage.value} {res.status.value if res else 'not reached'})")
    return None


def _person(user: str) -> str | None:
    if user.strip().lower() in NOT_A_PERSON or user.lower().startswith(SERVICE_PREFIXES):
        return f"gates are decided by a named person; {user or 'no user'!r} is not one (pass --user / X-ATH-User)"
    return None


def approval_problem(m: RunManifest, stage: Stage, user: str, groups: list[str], owners: list[str]) -> str | None:
    if p := _waiting(m, stage) or _person(user):
        return p
    if user == m.context.triggered_by:
        return (f"{user} triggered this run and ATH acted on their behalf, so they cannot approve its gates; "
                f"another member of {owners} must (separation of duties)")
    if not set(groups) & set(owners):
        return f"{user} is not in an owning group of {m.context.product} ({owners}); groups given: {groups or 'none'}"
    if any(g.stage == stage and g.decision == "approved" for g in m.gates):
        return f"gate {stage.value} is already approved"
    return None


def rejection_problem(m: RunManifest, stage: Stage, user: str, groups: list[str], owners: list[str]) -> str | None:
    if p := _waiting(m, stage) or _person(user):
        return p
    if user != m.context.triggered_by and not set(groups) & set(owners):
        return f"{user} neither triggered this run nor is in an owning group of {m.context.product} ({owners})"
    return None
