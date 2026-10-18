"""Jenkins adapters against an in-memory Jenkins (httpx.MockTransport), no network."""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable

import httpx
import pytest
import yaml

from ath_cli.main import main
from ath_core.adapters import AdapterError, ExecStatus, RawResults
from ath_core.adapters.parsers.jenkins_test_report import JenkinsTestReportParser
from ath_core.adapters.pipeline.jenkins import JenkinsJob, JenkinsSuiteExecutor, _Jenkins
from ath_core.masking import Masker
from ath_core.schemas import CaseStatus, RunContext, load_profile
from ath_server.orchestrator import Orchestrator, StateStore

BASE = "https://jenkins.test/ci"
AUTH = "Basic " + base64.b64encode(b"ath-reader:api-secret").decode()


def report(*cases: dict[str, Any]) -> dict[str, Any]:
    return {"suites": [{"name": "s", "cases": list(cases)}]}


def case(name: str, status: str = "PASSED", **kw: Any) -> dict[str, Any]:
    return {"className": "snx.SmokeIT", "name": name, "status": status, "duration": 0.5, **kw}


class FakeJenkins:
    """Just enough of Jenkins: webhook + API triggers, queue, builds, test reports, artifacts."""

    def __init__(self, report_for: Callable[[dict[str, str]], dict] | None = None,
                 result_for: Callable[[dict[str, str]], str] | None = None, queue_polls: int = 0):
        self.report_for = report_for or (lambda params: report(case("ok")))
        self.result_for = result_for or (lambda params: "SUCCESS")
        self.queue_polls = queue_polls
        self.queue: dict[int, dict[str, Any]] = {}
        self.builds: dict[str, dict[int, dict[str, Any]]] = {}
        self.triggers: list[tuple[str, dict[str, str]]] = []
        self.artifacts: dict[str, bytes] = {}
        self._next_q = 100

    def _enqueue(self, job: str, params: dict[str, str]) -> int:
        self._next_q += 1
        self.queue[self._next_q] = {"job": job, "params": params, "polls": 0}
        self.triggers.append((job, params))
        return self._next_q

    def _start(self, qid: int) -> int:
        q = self.queue.pop(qid)
        builds = self.builds.setdefault(q["job"], {})
        number = len(builds) + 1
        builds[number] = {"params": q["params"], "result": self.result_for(q["params"]),
                          "report": self.report_for(q["params"])}
        q["number"] = number
        self.queue[qid] = {**q, "started": True}
        return number

    @staticmethod
    def _params_action(params: dict[str, str]) -> list[dict]:
        return [{"parameters": [{"name": k, "value": v} for k, v in params.items()]}]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/ci")
        if path == "/generic-webhook-trigger/invoke":
            if req.headers.get("token") != "hook-secret":
                return httpx.Response(404, json={"jobs": None, "message": "Did not find any jobs"})
            params = json.loads(req.content)
            job = "settle/regression" if params.get("SUITE") == "regression" else "snx-smoke"
            if params.get("DEPLOY"):
                job = "snx-cd"
            qid = self._enqueue(job, params)
            return httpx.Response(200, json={"jobs": {job: {"triggered": True, "id": qid, "url": f"queue/item/{qid}/"}}})
        if req.headers.get("authorization") != AUTH:
            return httpx.Response(401)
        m = re.fullmatch(r"((?:/job/[^/]+)+)/buildWithParameters", path)
        if m and req.method == "POST":
            job = "/".join(m.group(1).split("/job/")[1:])
            params = dict(httpx.QueryParams(req.content.decode()))
            qid = self._enqueue(job, params)
            return httpx.Response(201, headers={"Location": f"{BASE}/queue/item/{qid}/"})
        if path == "/queue/api/json":
            items = [{"id": qid, "actions": self._params_action(q["params"])}
                     for qid, q in self.queue.items() if not q.get("started")]
            return httpx.Response(200, json={"items": items})
        m = re.fullmatch(r"/queue/item/(\d+)/api/json", path)
        if m:
            qid = int(m.group(1))
            q = self.queue.get(qid)
            if q is None:
                return httpx.Response(404)
            if not q.get("started"):
                q["polls"] += 1
                if q["polls"] <= self.queue_polls:
                    return httpx.Response(200, json={"why": "Waiting for next available executor"})
                self._start(qid)
            return httpx.Response(200, json={"executable": {"number": self.queue[qid]["number"]}})
        m = re.fullmatch(r"((?:/job/[^/]+)+)(?:/(\d+))?(/.*)?", path)
        if m:
            job = "/".join(m.group(1).split("/job/")[1:])
            builds = self.builds.get(job, {})
            number, rest = m.group(2), m.group(3) or ""
            if number is None and rest == "/api/json":
                return httpx.Response(200, json={"builds": [
                    {"number": n, "actions": self._params_action(b["params"])} for n, b in sorted(builds.items(), reverse=True)]})
            b = builds.get(int(number)) if number else None
            if b is None:
                return httpx.Response(404)
            if rest == "/api/json":
                return httpx.Response(200, json={"building": False, "result": b["result"],
                                                 "artifacts": [{"relativePath": p} for p in self.artifacts]})
            if rest == "/testReport/api/json":
                return httpx.Response(404) if b["report"] is None else httpx.Response(200, json=b["report"])
            if rest.startswith("/artifact/"):
                return httpx.Response(200, content=self.artifacts[rest.removeprefix("/artifact/")])
        return httpx.Response(404)


@pytest.fixture
def jenkins(monkeypatch):
    monkeypatch.setenv("T_JENKINS_TOKEN", "api-secret")
    monkeypatch.setenv("T_JENKINS_HOOK", "hook-secret")
    _Jenkins._attempts.clear()
    fake = FakeJenkins()
    monkeypatch.setattr(_Jenkins, "transport", httpx.MockTransport(fake))
    return fake


COMMON = {"baseUrl": BASE, "user": "ath-reader", "tokenRef": "env:T_JENKINS_TOKEN",
          "webhookTokenRef": "env:T_JENKINS_HOOK"}


def ctx(run_id: str = "r1") -> RunContext:
    return RunContext(run_id=run_id, product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                      triggered_by="tester")


def run_to_end(adapter, handle) -> ExecStatus:
    for _ in range(10):
        st = adapter.poll(handle)
        if st != ExecStatus.RUNNING:
            return st
    raise AssertionError("build never finished")


def test_webhook_trigger_follows_build_and_collects_test_report(jenkins):
    jenkins.queue_polls = 1
    ex = JenkinsSuiteExecutor(**COMMON, job="snx-smoke", params={"COMMIT_ID": "${commit}", "ENV": "${env}"})
    h = ex.start("smoke", ctx(), "mr001")
    job, params = jenkins.triggers[0]
    assert job == "snx-smoke"
    assert params == {"ATH_RUN_ID": "r1", "ATH_KEY": "r1:smoke:mr001:0", "COMMIT_ID": "mr001", "ENV": "uat1"}
    assert ex.poll(h) == ExecStatus.RUNNING            # still queued
    assert run_to_end(ex, h) == ExecStatus.DONE
    raw = ex.collect(h)
    assert raw.format == "jenkins-test-report" and "testReport.json" in raw.files


def test_api_trigger_uses_build_with_parameters(jenkins):
    deploy = JenkinsJob(**{**COMMON, "trigger": "api"}, job="settle/cd", params={"COMMIT_ID": "${commit}"})
    h = deploy.trigger(ctx(), "mr001")
    assert jenkins.triggers == [("settle/cd", {"ATH_RUN_ID": "r1", "ATH_KEY": "r1:settle/cd:mr001:0", "COMMIT_ID": "mr001"})]
    assert run_to_end(deploy, h) == ExecStatus.DONE


def test_resumed_stage_reuses_existing_build(jenkins):
    deploy = JenkinsJob(**COMMON, job="snx-cd", params={"DEPLOY": "1"})
    first = deploy.trigger(ctx(), "mr001")
    run_to_end(deploy, first)
    again = JenkinsJob(**COMMON, job="snx-cd", params={"DEPLOY": "1"}).trigger(ctx(), "mr001")
    assert len(jenkins.triggers) == 1
    assert again.meta["reused"] and again.meta["build"] == first.meta["build"]


def test_retries_are_new_attempts(jenkins):
    ex = JenkinsSuiteExecutor(**COMMON, job="snx-smoke")
    for _ in range(3):
        run_to_end(ex, ex.start("smoke", ctx(), "mr001"))
    assert [p["ATH_KEY"] for _, p in jenkins.triggers] == [f"r1:smoke:mr001:{i}" for i in range(3)]


def test_unstable_is_finished_for_suites_but_failed_for_deploy(jenkins):
    jenkins.result_for = lambda params: "UNSTABLE"
    ex = JenkinsSuiteExecutor(**COMMON, job="snx-smoke")
    assert run_to_end(ex, ex.start("smoke", ctx(), "mr001")) == ExecStatus.DONE
    deploy = JenkinsJob(**COMMON, job="snx-cd", params={"DEPLOY": "1"})
    assert run_to_end(deploy, deploy.trigger(ctx(), "mr001")) == ExecStatus.FAILED


def test_collect_archived_junit_artifacts(jenkins):
    jenkins.artifacts = {"target/failsafe-reports/TEST-a.xml": b"<testsuite/>", "build.log": b"x"}
    ex = JenkinsSuiteExecutor(**COMMON, job="settle/regression", params={"SUITE": "regression"},
                              collect="artifacts", artifacts="target/failsafe-reports/*.xml")
    h = ex.start("regression", ctx(), "mr001")
    run_to_end(ex, h)
    raw = ex.collect(h)
    assert raw.format == "junit-xml" and list(raw.files) == ["target/failsafe-reports/TEST-a.xml"]


def test_missing_test_report_explains_fix(jenkins):
    jenkins.report_for = lambda params: None
    ex = JenkinsSuiteExecutor(**COMMON, job="snx-smoke")
    h = ex.start("smoke", ctx(), "mr001")
    run_to_end(ex, h)
    with pytest.raises(AdapterError, match="junit"):
        ex.collect(h)


def test_bad_webhook_token_is_not_retryable(jenkins, monkeypatch):
    monkeypatch.setenv("T_JENKINS_HOOK", "wrong")
    with pytest.raises(AdapterError, match="no job accepted the token") as e:
        JenkinsSuiteExecutor(**COMMON, job="snx-smoke").start("smoke", ctx(), "mr001")
    assert not e.value.retryable


def test_config_problems_name_missing_secrets(monkeypatch):
    monkeypatch.delenv("T_JENKINS_TOKEN", raising=False)
    problems = JenkinsSuiteExecutor(baseUrl=BASE, job="j", user="u", tokenRef="env:T_JENKINS_TOKEN",
                                    trigger="api", collect="artifacts").config_problems()
    assert any("T_JENKINS_TOKEN is not set" in p for p in problems)
    assert any("artifacts" in p for p in problems)


def test_test_report_parser_maps_status_masks_and_correlates():
    body = {"childReports": [{"result": report(
        case("pays", "PASSED"),
        case("fixed", "FIXED"),
        case("settles", "REGRESSION", errorDetails="expected 1250.00 got 0.00 for card 4111111111111111",
             errorStackTrace="java.lang.AssertionError: expected 1250.00\n\tat snx.SmokeIT",
             stdout="start\nrrn=629014773311\n"),
        case("npe", "FAILED", errorStackTrace="java.lang.NullPointerException: x"),
        case("later", "SKIPPED", skipped=True),
    )}]}
    run = JenkinsTestReportParser().parse(RawResults("jenkins-test-report", {"r.json": json.dumps(body).encode()}),
                                          run_id="r1", product="p", suite="smoke", env="uat1", commit="c",
                                          masker=Masker())
    cases = run.by_id()
    assert [c.status for c in run.cases] == [CaseStatus.PASSED, CaseStatus.PASSED, CaseStatus.FAILED,
                                             CaseStatus.FAILED, CaseStatus.SKIPPED]
    settles = cases["snx.SmokeIT.settles"]
    assert "4111111111111111" not in settles.failure.message and settles.failure.type == "assertion"
    assert settles.correlation == {"rrn": "629014773311"}
    assert cases["snx.SmokeIT.npe"].failure.type == "java.lang.NullPointerException"


def test_report_only_run_end_to_end_against_jenkins(jenkins, tmp_path):
    """Deploy + smoke on the MR commit and merge-base + flaky retries, all through Jenkins."""
    def report_for(params):
        failing = params.get("COMMIT_ID") == "mr001"
        return report(case("ok"), case("settles", "FAILED" if failing else "PASSED", errorDetails="got 0.00"))
    jenkins.report_for = report_for
    profile_path = tmp_path / "p.yaml"
    profile_path.write_text(yaml.safe_dump({
        "apiVersion": "ainxt.io/test-profile/v1", "product": "settlenxt", "owners": ["settlement-dept"],
        "repos": {"code": "local"}, "build": {"adapter": "fake-pipeline"},
        "deploy": {"adapter": "jenkins-job", **COMMON, "job": "snx-cd",
                   "params": {"DEPLOY": "1", "COMMIT_ID": "${commit}"}},
        "suites": [{"id": "smoke", "executor": {"adapter": "jenkins-job", **COMMON, "job": "snx-smoke",
                                                "params": {"COMMIT_ID": "${commit}"}},
                    "results": {"format": "jenkins-test-report"}}],
        "selection": {"default": ["smoke"]},
        "logs": {"sources": [{"adapter": "fake-logs"}]},
        "policy": {"flakyRetries": 2, "environments": ["uat1"]},
    }))
    jenkins.result_for = lambda params: "SUCCESS" if params.get("DEPLOY") else "UNSTABLE"
    store = StateStore(tmp_path / "s.db")
    m = Orchestrator(load_profile(profile_path), store).start(ctx("e2e"))
    assert m.status.value == "succeeded"
    # deploy once, smoke on MR and merge-base, then two flaky retries on the MR commit
    assert [(j, p.get("COMMIT_ID")) for j, p in jenkins.triggers] == [
        ("snx-cd", "mr001"), ("snx-smoke", "mr001"), ("snx-smoke", "base000"),
        ("snx-smoke", "mr001"), ("snx-smoke", "mr001")]
    report_md = store.get_artifact("e2e", "report.md")
    assert "snx.SmokeIT.settles" in report_md and "BLOCKED" in report_md
    # resuming a finished run triggers nothing new
    Orchestrator(load_profile(profile_path), store).resume("e2e")
    assert len(jenkins.triggers) == 5


def test_doctor_reports_unset_secrets(monkeypatch, capsys):
    monkeypatch.delenv("SNX_JENKINS_API_TOKEN", raising=False)
    assert main(["doctor", "examples/settlenxt/test-profile.yaml"]) == 2
    assert "SNX_JENKINS_API_TOKEN is not set" in capsys.readouterr().out
