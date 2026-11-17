"""ATH API (FastAPI). The run lives here; desktop and server CLIs both call it.

Scaffold: in-process orchestrator, profiles loaded from a directory, a header stands in
for SSO. Production: validate the SSO JWT, map groups to roles, issue run-scoped tokens,
and execute stages as durable workflow activities.

Run locally:  ATH_PROFILES=examples/settlenxt uvicorn ath_server.api.app:app --reload
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ath_core.agents.cli import build_author_agent, build_triage_agents
from ath_core.config import load_config
from ath_core.schemas import RunContext, RunMode, Stage, load_profile
from ath_server.orchestrator import Orchestrator, StateStore

app = FastAPI(title="AiNxt Test Harness API", version="0.1.0")
os.makedirs(".ath", exist_ok=True)
STORE = StateStore(os.environ.get("ATH_DB", ".ath/api.db"))
PROFILE_DIR = Path(os.environ.get("ATH_PROFILES", "examples/settlenxt"))
CONFIG = load_config()  # server harness config: agent CLI for LLM stages ($ATH_CONFIG)


class RunRequest(BaseModel):
    product: str
    commit: str
    merge_base: str
    env: str
    mode: RunMode = RunMode.REPORT_ONLY
    mr: str | None = None
    jira: str | None = None
    changed_paths: list[str] = []
    profile_file: str = "test-profile.local.yaml"
    diff: str | None = None  # unified diff of the MR, for triage context
    tsd: str | None = None   # TSD markdown, for triage context
    # T3 test authoring: None follows the profile. The server has no test checkout yet, so T3 is skipped
    # here ("no test checkout given") until git-mr can clone the test repo.
    author: bool | None = None


class GateRequest(BaseModel):
    comment: str = ""
    # T3 approve: the plan hash, then the proposal hash, reviewed; refused (409) if the stored one differs
    ref: str | None = None


class AnswerItem(BaseModel):
    id: str              # Q-n
    answer: str = ""     # text, `accept` (take the model's assumption), or empty to skip


class AnswerRequest(BaseModel):
    """T3: answers to the plan's spec questions (T3 plans again), or an edited plan.yaml."""
    ref: str                              # the plan hash read; refused (409) if the stored one differs
    answers: list[AnswerItem] | None = None
    plan_yaml: str | None = None


# Run artifacts a person reads to review T3 (all masked); raw/ results and the rest are not served.
REVIEW_ARTIFACTS = {"author/plan.md", "author/plan.yaml", "author/answers.yaml", "author/changes.patch",
                    "author/checklist.md"}


def _user(x_ath_user: str | None) -> str:
    if not x_ath_user:  # stands in for SSO token validation
        raise HTTPException(401, "missing identity")
    return x_ath_user


def _orchestrator(product: str, profile_file: str) -> Orchestrator:
    path = (PROFILE_DIR / profile_file).resolve()
    if PROFILE_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, f"no profile {profile_file}")
    profile = load_profile(path)
    if profile.product != product:
        raise HTTPException(400, "product does not match profile")
    try:
        agents = build_triage_agents(CONFIG, profile)
    except ValueError as e:  # server harness config breaks a rule, e.g. model-family diversity
        raise HTTPException(500, f"server agent config: {e}")
    return Orchestrator(profile, STORE, agents=agents, profile_ref=profile_file, quorum=CONFIG.quorum,
                        author_factory=lambda skill: build_author_agent(CONFIG, profile, skill))


@app.post("/runs", status_code=202)
def create_run(req: RunRequest, bg: BackgroundTasks, x_ath_user: str | None = Header(None)) -> dict:
    user = _user(x_ath_user)
    orch = _orchestrator(req.product, req.profile_file)
    ctx = RunContext(run_id=f"ath-{uuid.uuid4().hex[:10]}", product=req.product, mode=req.mode, mr=req.mr,
                     jira=req.jira, commit=req.commit, merge_base=req.merge_base, env=req.env,
                     changed_paths=req.changed_paths, triggered_by=user, trigger_source="web", author=req.author)
    try:
        orch.check(ctx)  # refuse now: once queued, a refusal would only surface as a run that never appears
    except ValueError as e:
        raise HTTPException(400, str(e))
    context = {k: v for k, v in (("diff", req.diff), ("tsd", req.tsd)) if v}
    bg.add_task(orch.start, ctx, context)
    return {"run_id": ctx.run_id, "status": "queued"}


def _run_orchestrator(run_id: str) -> Orchestrator:
    """The orchestrator for an existing run, with the profile it was started with."""
    m = STORE.load_manifest(run_id)
    if m is None:
        raise HTTPException(404, "unknown run")
    ref = m.profile_ref or "test-profile.local.yaml"
    if not (PROFILE_DIR / ref).resolve().is_relative_to(PROFILE_DIR.resolve()) or not (PROFILE_DIR / ref).exists():
        raise HTTPException(409, f"run {run_id} was started with profile {ref!r}, which this server does not "
                                 f"serve (e.g. a CLI run); decide its gates where it was started")
    return _orchestrator(m.context.product, ref)


def _groups(x_ath_groups: str | None) -> list[str]:
    return [g.strip() for g in (x_ath_groups or "").split(",") if g.strip()]


@app.post("/runs/{run_id}/gates/T3/answer", status_code=202)
def answer_t3(run_id: str, req: AnswerRequest, bg: BackgroundTasks, x_ath_user: str | None = Header(None),
              x_ath_groups: str | None = Header(None)) -> dict:
    """Answer T3's spec questions, or send an edited plan. Not an approval: T3 plans again (answers)
    or stores the edited plan, and the run parks at the plan gate again."""
    user = _user(x_ath_user)
    orch = _run_orchestrator(run_id)
    answers = [a.model_dump() for a in req.answers] if req.answers else None
    try:
        orch.record_answer(run_id, user, _groups(x_ath_groups), req.ref, answers=answers, plan_yaml_text=req.plan_yaml)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    bg.add_task(orch.resume, run_id)  # plan again (answers) and park at the plan gate
    return {"run_id": run_id, "gate": "T3", "decision": "answered" if answers else "plan-edited"}


@app.post("/runs/{run_id}/gates/{gate}/{action}", status_code=202)
def decide_gate(run_id: str, gate: str, action: str, req: GateRequest, bg: BackgroundTasks,
                x_ath_user: str | None = Header(None), x_ath_groups: str | None = Header(None)) -> dict:
    """approve | reject a HITL gate. X-ATH-Groups stands in for the user's SSO groups."""
    user = _user(x_ath_user)
    if action not in ("approve", "reject") or gate not in ("T3", "T10", "T11"):
        raise HTTPException(400, "use /gates/{T3|T10|T11}/{approve|reject}")
    orch = _run_orchestrator(run_id)
    groups = _groups(x_ath_groups)
    decision = "approved" if action == "approve" else "rejected"
    try:
        orch.record_gate(run_id, Stage(gate), decision, user, groups, req.comment,
                         ref=req.ref if decision == "approved" else None)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if decision == "approved":
        bg.add_task(orch.resume, run_id)  # continue past the gate
    return {"run_id": run_id, "gate": gate, "decision": decision}


# Reads need an identity too: manifests, reports and events name people, commits and failures.
@app.get("/runs/{run_id}")
def get_run(run_id: str, x_ath_user: str | None = Header(None)) -> dict:
    _user(x_ath_user)
    m = STORE.load_manifest(run_id)
    if not m:
        raise HTTPException(404, "unknown run")
    return m.model_dump(mode="json")


@app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str, x_ath_user: str | None = Header(None)) -> str:
    _user(x_ath_user)
    body = STORE.get_artifact(run_id, "report.md")
    if body is None:
        raise HTTPException(404, "no report yet")
    return body


@app.get("/runs/{run_id}/artifacts/{name:path}", response_class=PlainTextResponse)
def get_artifact(run_id: str, name: str, x_ath_user: str | None = Header(None)) -> str:
    """A T3 review artifact (author/plan.md, plan.yaml, answers.yaml, changes.patch, checklist.md)."""
    _user(x_ath_user)
    if name not in REVIEW_ARTIFACTS:
        raise HTTPException(404, f"not a review artifact; use one of {sorted(REVIEW_ARTIFACTS)}")
    body = STORE.get_artifact(run_id, name)
    if not body:
        raise HTTPException(404, f"no {name} for run {run_id}")
    return body


@app.get("/runs/{run_id}/events")
def get_events(run_id: str, x_ath_user: str | None = Header(None)) -> list[dict]:
    _user(x_ath_user)
    return [{"stage": s, "kind": k, "detail": d} for s, k, d in STORE.events(run_id)]
