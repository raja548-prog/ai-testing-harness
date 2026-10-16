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

from ath_core.schemas import RunContext, RunMode, load_profile
from ath_server.orchestrator import Orchestrator, StateStore

app = FastAPI(title="AiNxt Test Harness API", version="0.1.0")
os.makedirs(".ath", exist_ok=True)
STORE = StateStore(os.environ.get("ATH_DB", ".ath/api.db"))
PROFILE_DIR = Path(os.environ.get("ATH_PROFILES", "examples/settlenxt"))


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


def _user(x_ath_user: str | None) -> str:
    if not x_ath_user:  # stands in for SSO token validation
        raise HTTPException(401, "missing identity")
    return x_ath_user


def _orchestrator(product: str, profile_file: str) -> Orchestrator:
    path = PROFILE_DIR / profile_file
    if not path.exists():
        raise HTTPException(404, f"no profile {profile_file}")
    profile = load_profile(path)
    if profile.product != product:
        raise HTTPException(400, "product does not match profile")
    return Orchestrator(profile, STORE)


@app.post("/runs", status_code=202)
def create_run(req: RunRequest, bg: BackgroundTasks, x_ath_user: str | None = Header(None)) -> dict:
    user = _user(x_ath_user)
    orch = _orchestrator(req.product, req.profile_file)
    ctx = RunContext(run_id=f"ath-{uuid.uuid4().hex[:10]}", product=req.product, mode=req.mode, mr=req.mr,
                     jira=req.jira, commit=req.commit, merge_base=req.merge_base, env=req.env,
                     changed_paths=req.changed_paths, triggered_by=user, trigger_source="web")
    bg.add_task(orch.start, ctx)
    return {"run_id": ctx.run_id, "status": "queued"}


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
