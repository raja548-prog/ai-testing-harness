import json

import pytest

from ath_core.adapters import registry
from ath_core.adapters.fake import FakeExecutor, FakeLock, FakePipeline
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


def test_unexpected_stage_error_fails_the_run_and_frees_the_env(orch, monkeypatch):
    def broken(ctx):
        raise ValueError("bad time '1,234.5' for card 4111111111111111")
    monkeypatch.setattr(orch, "t6_normalize", broken)
    m = orch.start(_ctx())
    assert m.status == RunStatus.FAILED
    assert m.stages[Stage.T6_NORMALIZE].error == "ValueError: bad time '1,234.5' for card [PAN****1111]"
    failed = [d for _, k, d in orch.store.events("r1") if k == "stage.failed"]
    assert failed == [{"error": m.stages[Stage.T6_NORMALIZE].error, "retryable": False}]
    assert orch.store.load_manifest("r1").status == RunStatus.FAILED
    assert FakeLock.held == {}


def test_error_outside_a_stage_still_fails_the_run_and_frees_the_env(orch, monkeypatch):
    event = orch.store.event

    def flaky_store(run_id, stage, kind, **detail):
        if (stage, kind) == ("T5", "stage.done"):
            raise RuntimeError("store unavailable")
        event(run_id, stage, kind, **detail)
    monkeypatch.setattr(orch.store, "event", flaky_store)
    with pytest.raises(RuntimeError, match="store unavailable"):
        orch.start(_ctx())
    assert orch.store.load_manifest("r1").status == RunStatus.FAILED
    assert FakeLock.held == {}



class _Answer:
    def __init__(self, name, category):
        self.name, self.category = name, category

    def classify(self, case, evidence):
        return self.category


@pytest.mark.parametrize("quorum, expected", [(None, TriageCategory.TEST_DATA), (3, TriageCategory.UNKNOWN)])
def test_configured_quorum_is_used_for_triage(tmp_path, quorum, expected):
    agents = [_Answer("a", TriageCategory.TEST_DATA), _Answer("b", TriageCategory.TEST_DATA),
              _Answer("c", TriageCategory.ENVIRONMENT)]
    o = Orchestrator(load_profile(LOCAL_PROFILE), StateStore(":memory:"), report_dir=tmp_path,
                     agents=agents, quorum=quorum)
    o.start(_ctx())
    assert _cats(o.store, "r1")["SNX-001"] == expected


def test_run_id_is_checked():
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            _ctx(bad)


def test_existing_run_id_with_other_inputs_is_refused(orch):
    orch.start(_ctx())
    with pytest.raises(ValueError, match="already exists with different inputs"):
        orch.start(_ctx(commit="mr002"))


def test_a_running_run_is_not_executed_twice(orch):
    orch.start(_ctx())
    orch.store.update_manifest("r1", lambda m: setattr(m, "status", RunStatus.RUNNING))  # e.g. another process
    with pytest.raises(ValueError, match="already running"):
        orch.resume("r1")
    assert orch.resume("r1", force=True).status == RunStatus.SUCCEEDED  # its process died: take it over


def test_resume_after_a_failure_takes_the_env_again_and_redeploys(orch, monkeypatch):
    t5 = orch.t5_execute

    def broken_once(ctx):
        monkeypatch.setattr(orch, "t5_execute", t5)
        raise RuntimeError("executor crashed")
    monkeypatch.setattr(orch, "t5_execute", broken_once)
    assert orch.start(_ctx()).status == RunStatus.FAILED and FakeLock.held == {}
    FakeLock.held["uat1"] = "other-run"  # someone else took the env and deployed their build
    m = orch.resume("r1")
    assert m.status == RunStatus.FAILED and "held by another run" in m.stages[Stage.T4_ENV].error
    FakeLock.held.clear()
    deploys = len(FakePipeline.triggered)
    assert orch.resume("r1").status == RunStatus.SUCCEEDED
    assert len(FakePipeline.triggered) == deploys + 1  # redeployed before running the suites
    # one redeploy decision: the first resume dropped the done T4; the second simply runs T4 again
    assert [k for _, k, _ in orch.store.events("r1")].count("env.redeploy") == 1


def test_resume_after_a_t6_failure_part_way_keeps_all_results(orch, monkeypatch):
    parse = orch._parse

    def fail_on_regression(raw, ctx, suite, commit):
        if suite == "regression" and commit == ctx.commit:
            raise RuntimeError("parser crashed")
        return parse(raw, ctx, suite, commit)
    monkeypatch.setattr(orch, "_parse", fail_on_regression)
    assert orch.start(_ctx()).status == RunStatus.FAILED
    monkeypatch.setattr(orch, "_parse", parse)
    assert orch.resume("r1").status == RunStatus.SUCCEEDED
    smoke = ATRFRun.model_validate_json(orch.store.get_artifact("r1", "atrf/smoke.json"))
    assert smoke.summary.total == 5  # not re-parsed from wiped raw results
    assert json.loads(orch.store.get_artifact("r1", "raw/regression")) == {}  # wiped once all parsed


def test_one_orchestrator_keeps_runs_apart(orch):
    orch.start(_ctx("r1"))
    orch.start(_ctx("r2", changed_paths=["README.md"]))  # matches no rule: the default suite, smoke
    assert orch.store.get_artifact("r2", "atrf/regression.json") is None
    assert "regression" not in orch.store.get_artifact("r2", "report.md")


class _StoppedEarly(FakeExecutor):
    def complete(self, handle):
        return False


def test_incomplete_suite_blocks_the_verdict_and_is_never_a_baseline(tmp_path, monkeypatch):
    monkeypatch.setitem(registry._kinds["executor"], "fake-executor", _StoppedEarly)
    o = Orchestrator(load_profile(LOCAL_PROFILE), StateStore(":memory:"), report_dir=tmp_path)
    m = o.start(_ctx())
    assert m.stages[Stage.T5_EXECUTE].output["incomplete"] == ["smoke", "regression"]
    assert o.store.get_baseline("settlenxt", "base000", "smoke") is None
    assert ATRFRun.model_validate_json(o.store.get_artifact("r1", "atrf/smoke.json")).incomplete
    report = o.store.get_artifact("r1", "report.md")
    assert "Verdict: BLOCKED" in report and "Incomplete results" in report
