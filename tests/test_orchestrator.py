import json

import pytest

from ath_core.adapters.fake import FakeLock, FakePipeline
from ath_core.schemas import (
    RunContext, RunMode, RunStatus, Stage, StageStatus, TriageCategory, load_profile,
)
from ath_core.schemas.atrf import ATRFRun
from ath_server.orchestrator import Orchestrator, StateStore
from tests.conftest import LOCAL_PROFILE


def _ctx(run_id="r1", **kw):
    base = dict(run_id=run_id, product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                changed_paths=["src/main/java/clearing/Dispute.java"], triggered_by="u123")
    base.update(kw)
    return RunContext(**base)


@pytest.fixture
def orch(tmp_path):
    return Orchestrator(load_profile(LOCAL_PROFILE), StateStore(":memory:"), report_dir=tmp_path)


def _cats(store, run_id, suite="smoke"):
    run = ATRFRun.model_validate_json(store.get_artifact(run_id, f"atrf/{suite}.json"))
    return {c.id: c.triage.category for c in run.failing()}


def test_report_only_end_to_end(orch):
    m = orch.start(_ctx())
    assert m.status == RunStatus.SUCCEEDED
    assert m.stages[Stage.T2_SELECT].output["suites"] == ["smoke", "regression"]
    assert _cats(orch.store, "r1") == {
        "SNX-001": TriageCategory.UNKNOWN, "SNX-002": TriageCategory.PRE_EXISTING,
        "SNX-003": TriageCategory.FLAKY, "SNX-004": TriageCategory.KNOWN_FAILURE,
    }
    report = orch.store.get_artifact("r1", "report.md")
    assert "Verdict: BLOCKED" in report and "4111" not in report.replace("[PAN****1111]", "")
    assert FakeLock.held == {}  # env hold released at the end


def test_raw_results_do_not_outlive_t6(orch):
    orch.start(_ctx())
    assert json.loads(orch.store.get_artifact("r1", "raw/smoke")) == {}


def test_baseline_is_cached_per_merge_base(orch):
    orch.start(_ctx("r1"))
    m2 = orch.start(_ctx("r2"))
    assert m2.stages[Stage.T5_EXECUTE].output["suites"]["smoke"] == "baseline-cached"


def test_env_outside_policy_is_refused(orch):
    with pytest.raises(ValueError, match="not allowed"):
        orch.start(_ctx(env="prod"))


def test_busy_env_fails_then_resumes(orch):
    FakeLock.held["uat1"] = "someone-else"
    m = orch.start(_ctx())
    assert m.status == RunStatus.FAILED
    assert m.stages[Stage.T4_ENV].status == StageStatus.FAILED
    FakeLock.held.clear()
    m = orch.resume("r1")
    assert m.status == RunStatus.SUCCEEDED
    kinds = [k for _, k, _ in orch.store.events("r1")]
    assert kinds.count("stage.done") == 9  # T1..T9 each completed exactly once overall


def test_resume_does_not_redo_finished_stages(orch):
    orch.start(_ctx())
    triggered = list(FakePipeline.triggered)
    orch.resume("r1")
    assert FakePipeline.triggered == triggered  # no second deploy


class _Regression:
    def __init__(self, name):
        self.name = name

    def classify(self, case, evidence):
        return TriageCategory.CODE_REGRESSION


def test_assist_mode_parks_at_the_redeploy_gate_for_a_regression(tmp_path):
    profile = load_profile(LOCAL_PROFILE)
    o = Orchestrator(profile, StateStore(":memory:"), report_dir=tmp_path,
                     agents=[_Regression(n) for n in "abc"])
    m = o.start(_ctx(mode=RunMode.ASSIST))
    assert m.status == RunStatus.WAITING_GATE
    assert m.stages[Stage.T10_REMEDIATE].status == StageStatus.WAITING_GATE
    assert FakeLock.held == {"uat1": "r1"}  # hold kept across the gate for the fix loop


def test_assist_mode_without_regressions_needs_no_gate(tmp_path):
    o = Orchestrator(load_profile(LOCAL_PROFILE), StateStore(":memory:"), report_dir=tmp_path)
    m = o.start(_ctx(mode=RunMode.ASSIST))  # no agents: the failure is `unknown`, nothing to fix
    assert m.status == RunStatus.SUCCEEDED
    assert m.stages[Stage.T10_REMEDIATE].output["reason"] == "no code-regression failures to remediate"


def test_log_source_fallback_is_recorded(orch):
    orch.start(_ctx())
    ev = [(k, d) for _, k, d in orch.store.events("r1") if k == "logsource.fallback"]
    assert ev and ev[0][1]["adapter"] == "log-api"
