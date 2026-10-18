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

from ath_core.agents.cli import build_triage_agents
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


class GateRequest(BaseModel):
    comment: str = ""


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
    return Orchestrator(profile, STORE, agents=agents, profile_ref=profile_file)


@app.post("/runs", status_code=202)
def create_run(req: RunRequest, bg: BackgroundTasks, x_ath_user: str | None = Header(None)) -> dict:
    user = _user(x_ath_user)
    orch = _orchestrator(req.product, req.profile_file)
    ctx = RunContext(run_id=f"ath-{uuid.uuid4().hex[:10]}", product=req.product, mode=req.mode, mr=req.mr,
                     jira=req.jira, commit=req.commit, merge_base=req.merge_base, env=req.env,
                     changed_paths=req.changed_paths, triggered_by=user, trigger_source="web")
    context = {k: v for k, v in (("diff", req.diff), ("tsd", req.tsd)) if v}
    bg.add_task(orch.start, ctx, context)
    return {"run_id": ctx.run_id, "status": "queued"}


@app.post("/runs/{run_id}/gates/{gate}/{action}", status_code=202)
def decide_gate(run_id: str, gate: str, action: str, req: GateRequest, bg: BackgroundTasks,
                x_ath_user: str | None = Header(None), x_ath_groups: str | None = Header(None)) -> dict:
    """approve | reject a HITL gate. X-ATH-Groups stands in for the user's SSO groups."""
    user = _user(x_ath_user)
    if action not in ("approve", "reject") or gate not in ("T3", "T10", "T11"):
        raise HTTPException(400, "use /gates/{T3|T10|T11}/{approve|reject}")
    m = STORE.load_manifest(run_id)
    if m is None:
        raise HTTPException(404, "unknown run")
    orch = _orchestrator(m.context.product, m.profile_ref or "test-profile.local.yaml")
    groups = [g.strip() for g in (x_ath_groups or "").split(",") if g.strip()]
    decision = "approved" if action == "approve" else "rejected"
    try:
        orch.record_gate(run_id, Stage(gate), decision, user, groups, req.comment)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if decision == "approved":
        bg.add_task(orch.resume, run_id)  # continue past the gate
    return {"run_id": run_id, "gate": gate, "decision": decision}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    m = STORE.load_manifest(run_id)
    if not m:
        raise HTTPException(404, "unknown run")
    return m.model_dump(mode="json")


@app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    body = STORE.get_artifact(run_id, "report.md")
    if body is None:
        raise HTTPException(404, "no report yet")
    return body


@app.get("/runs/{run_id}/events")
def get_events(run_id: str) -> list[dict]:
    return [{"stage": s, "kind": k, "detail": d} for s, k, d in STORE.events(run_id)]
