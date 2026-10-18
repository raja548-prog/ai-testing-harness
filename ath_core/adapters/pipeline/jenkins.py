"""Jenkins adapters: trigger a job, follow it to completion, collect its test results.

Each product points at its own Jenkins. Profile params (secrets by reference only):

  baseUrl          https://jenkins.example.internal (a context path like /jenkins is fine)
  job              job name; folders as "folder/sub/job"
  trigger          webhook (default) | api
  webhookTokenRef  webhook mode: env:NAME holding the job's Generic Webhook Trigger token
  user, tokenRef   Jenkins user with read access and env:NAME holding its API token
  params           build variables; values may use ${commit} ${env} ${runId} ${suite} ${product}
  collect          executor only: test-report (default) | artifacts
  artifacts        collect=artifacts: glob of archived JUnit files, e.g. target/surefire-reports/*.xml

Triggering:
  webhook  POST {baseUrl}/generic-webhook-trigger/invoke with the job's token, the same way
           Git pushes trigger it today (Generic Webhook Trigger plugin). The JSON body
           carries the variables; the job maps them with JSONPath, e.g. COMMIT_ID <- $.COMMIT_ID.
  api      POST {baseUrl}/job/<job>/buildWithParameters with the API token.

Following a build and collecting results always use the read API: a webhook token can
start a job but cannot read it, so user + tokenRef are needed in both modes.

Results:
  test-report  GET <build>/testReport/api/json, i.e. what the job's `junit` step published.
               Parse with results.format: jenkins-test-report.
  artifacts    Download archived files matching `artifacts`; parse with results.format:
               junit-xml. Use this when tests carry ids, tags or correlation keys in
               <properties>, which Jenkins' test report drops.

Idempotency: every build gets ATH_RUN_ID and ATH_KEY (run:label:commit:attempt). Before
triggering, the adapter looks for a queued or recent build with the same ATH_KEY and
follows it instead, so a resumed stage never starts a second deploy. For that lookup the
job must declare ATH_RUN_ID and ATH_KEY as string parameters.
"""
from __future__ import annotations

import fnmatch
from string import Template
from typing import Any
from urllib.parse import quote

import httpx

from ath_core.adapters.base import (
    AdapterError, ExecStatus, ExecutionHandle, ExecutorAdapter, PipelineAdapter, RawResults, registry,
)
from ath_core.schemas.run import RunContext
from ath_core.secrets import resolve_secret, secret_problem

TRIGGER_MODES = ("webhook", "api")
COLLECT_MODES = ("test-report", "artifacts")
RECENT_BUILDS = 30


def _job_path(job: str) -> str:
    return "".join(f"/job/{quote(part, safe='')}" for part in job.strip("/").split("/"))


def _parameters(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for action in item.get("actions") or []:
        for p in (action or {}).get("parameters") or []:
            out[p.get("name")] = p.get("value")
    return out


class _Jenkins:
    """Shared Jenkins plumbing for the pipeline and executor adapters."""

    params: dict[str, Any]
    transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport
    unstable_is_success = False                   # UNSTABLE means "tests failed" for suites
    _attempts: dict[tuple[str, str, str], int] = {}

    # ------------------------------------------------------------- configuration
    def config_problems(self) -> list[str]:
        """Problems that stop this adapter from working; checked by `doctor` and before calls."""
        p = self.params
        problems = [f"jenkins-job: '{k}' missing" for k in ("baseUrl", "job", "user") if not p.get(k)]
        mode = p.get("trigger", "webhook")
        if mode not in TRIGGER_MODES:
            problems.append(f"jenkins-job: trigger must be one of {TRIGGER_MODES}, got {mode!r}")
        if err := secret_problem(p.get("tokenRef"), "jenkins-job API token (tokenRef)"):
            problems.append(err)
        if mode == "webhook" and (err := secret_problem(p.get("webhookTokenRef"), "jenkins-job webhook token (webhookTokenRef)")):
            problems.append(err)
        return problems

    def _client(self) -> httpx.Client:
        if problems := self.config_problems():
            raise AdapterError("; ".join(problems), retryable=False)
        token = resolve_secret(self.params["tokenRef"], "jenkins-job API token")
        return httpx.Client(base_url=self.params["baseUrl"].rstrip("/"), auth=(self.params["user"], token),
                            timeout=30, transport=self.transport)

    @property
    def _job(self) -> str:
        return self.params["job"]

    # ------------------------------------------------------------- HTTP helpers
    def _call(self, c: httpx.Client, method: str, path: str, **kw: Any) -> httpx.Response | None:
        """One HTTP call. Returns None on 404; raises AdapterError on anything else that failed."""
        try:
            r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise AdapterError(f"jenkins {method} {path}: {e}")
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise AdapterError(f"jenkins {method} {path}: HTTP {r.status_code}; check user/tokenRef "
                               f"and the user's permissions on {self._job}", retryable=False)
        if r.status_code >= 400:
            raise AdapterError(f"jenkins {method} {path}: HTTP {r.status_code}", retryable=r.status_code >= 500)
        return r

    def _json(self, c: httpx.Client, path: str, **params: Any) -> dict[str, Any] | None:
        r = self._call(c, "GET", path, params=params or None)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            raise AdapterError(f"jenkins GET {path}: response is not JSON")

    # ------------------------------------------------------------- trigger
    def _variables(self, ctx: RunContext, label: str, commit: str, key: str,
                   extra: dict[str, Any] | None = None) -> dict[str, str]:
        values = {"commit": commit, "env": ctx.env, "runId": ctx.run_id, "suite": label, "product": ctx.product}
        out = {"ATH_RUN_ID": ctx.run_id, "ATH_KEY": key}
        for name, value in {**(self.params.get("params") or {}), **(extra or {})}.items():
            out[name] = Template(value).safe_substitute(values) if isinstance(value, str) else str(value)
        return out

    def _start(self, ctx: RunContext, label: str, commit: str, attempt: int,
               extra: dict[str, Any] | None = None) -> ExecutionHandle:
        key = f"{ctx.run_id}:{label}:{commit}:{attempt}"
        handle = ExecutionHandle("jenkins-job", f"{self._job}@{key}", ctx.run_id, label, commit, {"key": key})
        with self._client() as c:
            found = self._find(c, key)
            if found:
                handle.meta.update(found, reused=True)
                return handle
            variables = self._variables(ctx, label, commit, key, extra)
            if self.params.get("trigger", "webhook") == "api":
                handle.meta["queue_id"] = self._trigger_api(c, variables)
            else:
                handle.meta["queue_id"] = self._trigger_webhook(c, variables)
        return handle

    def _trigger_api(self, c: httpx.Client, variables: dict[str, str]) -> int | None:
        r = self._call(c, "POST", f"{_job_path(self._job)}/buildWithParameters", data=variables)
        if r is None:
            raise AdapterError(f"jenkins job {self._job} not found (or it has no parameters)", retryable=False)
        return _queue_id(r.headers.get("Location", ""))

    def _trigger_webhook(self, c: httpx.Client, variables: dict[str, str]) -> int | None:
        token = resolve_secret(self.params.get("webhookTokenRef"), "jenkins-job webhook token")
        r = self._call(c, "POST", "/generic-webhook-trigger/invoke", json=variables, headers={"token": token})
        if r is None:
            raise AdapterError("jenkins webhook: no job accepted the token; check webhookTokenRef and the "
                               "job's Generic Webhook Trigger configuration", retryable=False)
        try:
            jobs = (r.json() or {}).get("jobs") or {}
        except ValueError:
            jobs = {}
        short = self._job.strip("/").split("/")[-1]
        entry = jobs.get(self._job.strip("/")) or jobs.get(short)
        if entry is None and len(jobs) == 1:
            entry = next(iter(jobs.values()))
        if not entry or not entry.get("triggered"):
            raise AdapterError(f"jenkins webhook did not trigger {self._job}: {sorted(jobs) or 'no jobs'}",
                               retryable=False)
        qid = entry.get("id")
        return int(qid) if qid is not None else _queue_id(str(entry.get("url", "")))

    def _find(self, c: httpx.Client, key: str) -> dict[str, Any] | None:
        """Locate a queued or recent build carrying this ATH_KEY."""
        job = self._json(c, f"{_job_path(self._job)}/api/json",
                         tree=f"builds[number,actions[parameters[name,value]]]{{0,{RECENT_BUILDS}}}")
        for b in (job or {}).get("builds") or []:
            if _parameters(b).get("ATH_KEY") == key:
                return {"build": int(b["number"])}
        queue = self._json(c, "/queue/api/json", tree="items[id,actions[parameters[name,value]]]")
        for item in (queue or {}).get("items") or []:
            if _parameters(item).get("ATH_KEY") == key:
                return {"queue_id": int(item["id"])}
        return None

    # ------------------------------------------------------------- follow
    def _poll(self, handle: ExecutionHandle) -> ExecStatus:
        with self._client() as c:
            if "build" not in handle.meta:
                state = self._leave_queue(c, handle)
                if state is not None:
                    return state
            b = self._json(c, self._build_path(handle) + "/api/json", tree="building,result")
            if b is None:
                raise AdapterError(f"jenkins build {self._job} #{handle.meta['build']} disappeared")
            if b.get("building") or b.get("result") is None:
                return ExecStatus.RUNNING
            result = b["result"]
            if result == "SUCCESS" or (result == "UNSTABLE" and self.unstable_is_success):
                return ExecStatus.DONE
            handle.meta["result"] = result
            return ExecStatus.FAILED

    def _leave_queue(self, c: httpx.Client, handle: ExecutionHandle) -> ExecStatus | None:
        """Resolve the queue item to a build number. Returns a status while there is no build yet."""
        qid = handle.meta.get("queue_id")
        item = self._json(c, f"/queue/item/{qid}/api/json") if qid is not None else None
        if item is not None:
            if item.get("cancelled"):
                handle.meta["result"] = "CANCELLED"
                return ExecStatus.FAILED
            number = (item.get("executable") or {}).get("number")
            if number is not None:
                handle.meta["build"] = int(number)
                return None
            return ExecStatus.RUNNING
        # Queue items expire a few minutes after the build starts; fall back to the build list.
        found = self._find(c, handle.meta["key"])
        if found and "build" in found:
            handle.meta["build"] = found["build"]
            return None
        return ExecStatus.RUNNING

    def _build_path(self, handle: ExecutionHandle) -> str:
        return f"{_job_path(self._job)}/{handle.meta['build']}"

    def _cancel(self, handle: ExecutionHandle) -> None:
        with self._client() as c:
            if "build" in handle.meta:
                self._call(c, "POST", self._build_path(handle) + "/stop")
            elif handle.meta.get("queue_id") is not None:
                self._call(c, "POST", "/queue/cancelItem", params={"id": handle.meta["queue_id"]})


def _queue_id(url: str) -> int | None:
    parts = [p for p in url.rstrip("/").split("/") if p]
    if len(parts) >= 2 and parts[-2] == "item" and parts[-1].isdigit():
        return int(parts[-1])
    return None


@registry.register("pipeline", "jenkins-job")
class JenkinsJob(_Jenkins, PipelineAdapter):
    """CI/CD job, e.g. the CD job that deploys a commit to the UAT environment."""

    def trigger(self, ctx: RunContext, commit: str, extra: dict[str, Any] | None = None) -> ExecutionHandle:
        return self._start(ctx, self._job, commit, 0, extra)

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        return self._poll(handle)


@registry.register("executor", "jenkins-job")
class JenkinsSuiteExecutor(_Jenkins, ExecutorAdapter):
    """Test-suite job. UNSTABLE (tests failed) still counts as a finished run."""

    unstable_is_success = True

    def config_problems(self) -> list[str]:
        problems = super().config_problems()
        collect = self.params.get("collect", "test-report")
        if collect not in COLLECT_MODES:
            problems.append(f"jenkins-job: collect must be one of {COLLECT_MODES}, got {collect!r}")
        if collect == "artifacts" and not self.params.get("artifacts"):
            problems.append("jenkins-job: collect=artifacts needs an 'artifacts' glob")
        return problems

    def start(self, suite: str, ctx: RunContext, commit: str) -> ExecutionHandle:
        # Each start for the same run, suite and commit is a new attempt (T7 flaky retries).
        k = (ctx.run_id, suite, commit)
        attempt = _Jenkins._attempts.get(k, 0)
        _Jenkins._attempts[k] = attempt + 1
        return self._start(ctx, suite, commit, attempt)

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        return self._poll(handle)

    def cancel(self, handle: ExecutionHandle) -> None:
        self._cancel(handle)

    def collect(self, handle: ExecutionHandle) -> RawResults:
        if "build" not in handle.meta:
            raise AdapterError(f"jenkins {self._job}: no build to collect results from", retryable=False)
        with self._client() as c:
            if self.params.get("collect", "test-report") == "artifacts":
                return self._collect_artifacts(c, handle)
            r = self._call(c, "GET", self._build_path(handle) + "/testReport/api/json")
            if r is None:
                raise AdapterError(f"jenkins {self._job} #{handle.meta['build']} published no test report; "
                                   f"add a `junit` step to the job or use collect: artifacts", retryable=False)
            return RawResults(format="jenkins-test-report", files={"testReport.json": r.content})

    def _collect_artifacts(self, c: httpx.Client, handle: ExecutionHandle) -> RawResults:
        pattern = self.params["artifacts"]
        build = self._json(c, self._build_path(handle) + "/api/json", tree="artifacts[relativePath]") or {}
        paths = [a["relativePath"] for a in build.get("artifacts") or [] if fnmatch.fnmatch(a["relativePath"], pattern)]
        if not paths:
            raise AdapterError(f"jenkins {self._job} #{handle.meta['build']}: no archived files match {pattern!r}",
                               retryable=False)
        files: dict[str, bytes] = {}
        for rel in paths:
            r = self._call(c, "GET", f"{self._build_path(handle)}/artifact/{quote(rel)}")
            if r is not None:
                files[rel] = r.content
        return RawResults(format="junit-xml", files=files)
