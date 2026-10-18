"""HITL gates: approve and reject, separation of duties, CLI and API."""
import importlib

import pytest
from fastapi.testclient import TestClient

from ath_cli.main import main
from ath_core.adapters.fake import FakeLock
from ath_core.schemas import RunContext, RunMode, RunStatus, Stage, StageStatus, TriageCategory, load_profile
from ath_server.orchestrator import Orchestrator, StateStore
from tests.conftest import LOCAL_PROFILE

OWNERS = ["settlement-dept"]


class Regression:
    def __init__(self, name):
        self.name = name

    def classify(self, case, evidence):
        return TriageCategory.CODE_REGRESSION


def _orch(store=None):
    return Orchestrator(load_profile(LOCAL_PROFILE), store or StateStore(":memory:"),
                        agents=[Regression(n) for n in "abc"], profile_ref=str(LOCAL_PROFILE))


@pytest.fixture
def parked():
    o = _orch()
    ctx = RunContext(run_id="g1", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                     mode=RunMode.ASSIST, triggered_by="dev-asha")
    m = o.start(ctx)
    assert m.status == RunStatus.WAITING_GATE and m.stages[Stage.T10_REMEDIATE].status == StageStatus.WAITING_GATE
    return o


def test_owner_approval_continues_the_run(parked):
    m = parked.approve("g1", Stage.T10_REMEDIATE, "lead-ravi", OWNERS, "fix looks right")
    assert m.status == RunStatus.SUCCEEDED
    assert m.gates[0].by == "lead-ravi" and m.gates[0].decision == "approved"
    assert "approved by lead-ravi" in m.stages[Stage.T10_REMEDIATE].output["reason"]
    assert FakeLock.held == {}  # hold released at the end
    kinds = [k for _, k, _ in parked.store.events("g1")]
    assert "gate.wait" in kinds and "gate.approved" in kinds


@pytest.mark.parametrize("user, groups, match", [
    ("dev-asha", OWNERS, "triggered this run"),                  # the requester is the author
    ("local-user", OWNERS, "named person"),                     # CLI default identity
    ("ath-service", OWNERS, "named person"),                    # service identity
    ("lead-ravi", ["payments-dept"], "not in an owning group"),
])
def test_separation_of_duties(parked, user, groups, match):
    with pytest.raises(PermissionError, match=match):
        parked.approve("g1", Stage.T10_REMEDIATE, user, groups)
    assert parked.store.load_manifest("g1").status == RunStatus.WAITING_GATE  # nothing recorded


def test_only_the_waiting_gate_can_be_decided(parked):
    with pytest.raises(PermissionError, match="not waiting at gate T3"):
        parked.approve("g1", Stage.T3_AUTHOR, "lead-ravi", OWNERS)


def test_reject_stops_the_run_and_keeps_artifacts(parked):
    m = parked.reject("g1", Stage.T10_REMEDIATE, "dev-asha", [], "wrong fix")  # the requester may stop it
    assert m.status == RunStatus.REJECTED
    assert m.stages[Stage.T10_REMEDIATE].status == StageStatus.REJECTED
    assert "rejected by dev-asha: wrong fix" in m.stages[Stage.T10_REMEDIATE].error
    assert parked.store.get_artifact("g1", "report.md") and FakeLock.held == {}
    with pytest.raises(ValueError, match="rejected"):
        parked.resume("g1")
    with pytest.raises(PermissionError, match="not waiting"):
        parked.approve("g1", Stage.T10_REMEDIATE, "lead-ravi", OWNERS)


def test_outsider_cannot_reject(parked):
    with pytest.raises(PermissionError, match="neither triggered"):
        parked.reject("g1", Stage.T10_REMEDIATE, "someone", ["payments-dept"])


def test_decision_with_a_changed_profile_is_refused(parked):
    other = load_profile(LOCAL_PROFILE).model_copy(update={"owners": ["settlement-dept", "everyone"]})
    with pytest.raises(ValueError, match="profile changed"):
        Orchestrator(other, parked.store).approve("g1", Stage.T10_REMEDIATE, "lead-ravi", OWNERS)


def test_cli_approve_and_reject(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    o = _orch(store)
    o.start(RunContext(run_id="c1", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                       mode=RunMode.ASSIST, triggered_by="dev-asha"))
    common = ["--profile", str(LOCAL_PROFILE), "--db", db]
    assert main(["approve", "c1", "--gate", "T10", "--user", "dev-asha", "--groups", "settlement-dept", *common]) == 1
    assert "triggered this run" in capsys.readouterr().out
    assert main(["approve", "c1", "--gate", "T10", "--user", "lead-ravi", "--groups", "settlement-dept",
                 "--comment", "ok", *common]) == 0
    assert "run c1: succeeded" in capsys.readouterr().out


def test_api_gate_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("ATH_DB", str(tmp_path / "api.db"))
    import ath_server.api.app as appmod
    importlib.reload(appmod)
    monkeypatch.setattr(appmod, "build_triage_agents", lambda cfg, profile: [Regression(n) for n in "abc"])
    client = TestClient(appmod.app)
    body = {"product": "settlenxt", "commit": "mr001", "merge_base": "base000", "env": "uat1", "mode": "assist"}
    run_id = client.post("/runs", json=body, headers={"X-ATH-User": "dev-asha"}).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting-gate"
    url = f"/runs/{run_id}/gates/T10"
    r = client.post(f"{url}/approve", json={"comment": "mine"},
                    headers={"X-ATH-User": "dev-asha", "X-ATH-Groups": "settlement-dept"})
    assert r.status_code == 403 and "triggered this run" in r.json()["detail"]
    assert client.post(f"{url}/approve", json={}).status_code == 401
    r = client.post(f"{url}/approve", json={"comment": "ok"},
                    headers={"X-ATH-User": "lead-ravi", "X-ATH-Groups": "settlement-dept"})
    assert r.status_code == 202 and r.json()["decision"] == "approved"
    assert client.get(f"/runs/{run_id}").json()["status"] == "succeeded"
    assert client.post(f"/runs/{run_id}/gates/T99/approve", json={},
                       headers={"X-ATH-User": "lead-ravi"}).status_code == 400
