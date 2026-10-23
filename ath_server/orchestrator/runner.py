"""ATH orchestrator: runs stages in order, checkpoints each one, enforces mode and gates.

This in-process runner is the reference implementation of the run semantics. In the
ATH service the same stage functions are wrapped as workflow activities (e.g. Temporal),
which adds durable timers, webhook signals and multi-day gate waits.

Scaffold status:
- Implemented: T2 selection, T4 env hold + deploy, T5 execute (MR + cached merge-base
  baseline), T6 normalize with masking, T7 pre-checks + pluggable agent voting, T8 quorum,
  T9 report, resume from checkpoints, structured events.
- Stubbed: T1 impact (LLM), T3 authoring (LLM + gate), T10/T11 fix loop.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ath_core.adapters import AdapterError, ExecStatus, MaskedLogs, RawResults, registry
from ath_core.adapters import loader  # noqa: F401  (registers built-in adapters)
from ath_core.context import diff_excerpt, tsd_excerpt
from ath_core.gates import approval_problem, rejection_problem
from ath_core.masking import Masker
from ath_core.report.markdown import render_report
from ath_core.schemas.atrf import ATRFCase, ATRFRun, Triage, TriageCategory
from ath_core.schemas.profile import TestProfile
from ath_core.schemas.run import (
    GateDecision, RunContext, RunManifest, RunMode, RunStatus, Stage, StageResult, StageStatus, utcnow,
)
from ath_core.stages.select import select_suites
from ath_core.triage.precheck import apply_prechecks, mark_flaky
from ath_core.triage.voting import TriageAgent, vote
from ath_core.version import CORE_VERSION

from .store import StateStore

# Status checks on external jobs back off: the first comes after FIRST_POLL_SEC, each later gap is 1.5x
# longer, up to the adapter's `pollSec` (default DEFAULT_POLL_SEC). Short jobs are noticed quickly; a
# 3-hour suite costs a few hundred requests to the product's Jenkins, not thousands.
FIRST_POLL_SEC = 2.0
DEFAULT_POLL_SEC = 30.0
POLL_BACKOFF = 1.5


class GateRequired(Exception):
    """Raised when a run reaches a HITL gate; the run parks until approved."""


class Orchestrator:
    def __init__(self, profile: TestProfile, store: StateStore, package_version: str = "dev",
                 agents: list[TriageAgent] | None = None, quarantined: set[str] | None = None,
                 report_dir: str | Path | None = None, profile_ref: str | None = None):
        self.profile = profile
        self.profile_ref = profile_ref
        self.store = store
        self.package_version = package_version
        self.agents = agents or []
        self.quarantined = quarantined or set()
        self.report_dir = Path(report_dir) if report_dir else None
        self.masker = Masker(categories=profile.logs.masking, correlation_keys=profile.correlation_keys())
        self._atrf: dict[str, ATRFRun] = {}
        self._baseline: dict[str, ATRFRun] = {}
        self._sleep: Callable[[float], None] = time.sleep  # injectable, so tests need not wait
        self._clock: Callable[[], float] = time.monotonic

    # ---------------------------------------------------------------- public API
    def start(self, ctx: RunContext, context: dict[str, str] | None = None) -> RunManifest:
        """Start (or continue) a run. `context` holds change context for LLM stages:
        {"diff": unified diff, "tsd": TSD markdown}. It is masked and stored with the run."""
        self._guard(ctx)
        m = self.store.load_manifest(ctx.run_id)
        if m is None:
            m = RunManifest(context=ctx, core_version=CORE_VERSION, package_version=self.package_version,
                            profile_hash=self._profile_hash(), profile_ref=self.profile_ref)
            for name, text in (context or {}).items():
                masked = self.masker.mask(text)
                self.store.put_artifact(ctx.run_id, f"context/{name}", masked)
                m.inputs[name] = hashlib.sha256(masked.encode()).hexdigest()
        return self._execute(m)

    def resume(self, run_id: str) -> RunManifest:
        m = self._load(run_id)
        if m.status == RunStatus.REJECTED:
            raise ValueError(f"run {run_id} was rejected at a gate and cannot be resumed")
        return self._execute(m)

    def approve(self, run_id: str, stage: Stage, user: str, groups: list[str], comment: str = "") -> RunManifest:
        """Record an approval (separation of duties applies), then continue the run past the gate."""
        return self._execute(self.record_gate(run_id, stage, "approved", user, groups, comment))

    def reject(self, run_id: str, stage: Stage, user: str, groups: list[str], comment: str = "") -> RunManifest:
        """Stop the run at a gate. Artifacts are kept; the env hold is released."""
        return self.record_gate(run_id, stage, "rejected", user, groups, comment)

    def record_gate(self, run_id: str, stage: Stage, decision: str, user: str, groups: list[str],
                    comment: str = "") -> RunManifest:
        """Check and record a gate decision without running anything (the API resumes in the background).

        Raises PermissionError when the decider may not decide, ValueError if the profile changed."""
        m = self._load(run_id)
        if m.profile_hash != self._profile_hash():
            raise ValueError(f"the profile changed since run {run_id} started; decide with the profile it ran with")
        rule = approval_problem if decision == "approved" else rejection_problem
        if problem := rule(m, stage, user, groups, self.profile.owners):
            raise PermissionError(problem)
        m.gates.append(GateDecision(stage=stage, decision=decision, by=user, groups=groups, comment=comment))
        self.store.event(run_id, stage.value, f"gate.{decision}", by=user, groups=groups, comment=comment)
        if decision == "rejected":
            res = m.stages[stage]
            res.status, res.error, res.finished_at = StageStatus.REJECTED, f"rejected by {user}: {comment}", utcnow()
            self.store.checkpoint(run_id, res)
            m.status = RunStatus.REJECTED
            self._release_env(m.context)
        self.store.save_manifest(m)
        return m

    def _load(self, run_id: str) -> RunManifest:
        m = self.store.load_manifest(run_id)
        if m is None:
            raise KeyError(f"unknown run {run_id}")
        return m

    def _profile_hash(self) -> str:
        return hashlib.sha256(self.profile.model_dump_json().encode()).hexdigest()[:16]

    def _approval(self, stage: Stage) -> GateDecision | None:
        return next((g for g in self._m.gates if g.stage == stage and g.decision == "approved"), None)

    # ---------------------------------------------------------------- core loop
    def _plan(self, mode: RunMode) -> list[tuple[Stage, Callable[[RunContext], dict[str, Any]]]]:
        plan = [
            (Stage.T1_IMPACT, self.t1_impact), (Stage.T2_SELECT, self.t2_select),
            (Stage.T3_AUTHOR, self.t3_author), (Stage.T4_ENV, self.t4_env),
            (Stage.T5_EXECUTE, self.t5_execute), (Stage.T6_NORMALIZE, self.t6_normalize),
            (Stage.T7_TRIAGE, self.t7_triage), (Stage.T8_VERIFY, self.t8_verify),
            (Stage.T9_REPORT, self.t9_report),
        ]
        if mode != RunMode.REPORT_ONLY:
            plan += [(Stage.T10_REMEDIATE, self.t10_remediate), (Stage.T11_VALIDATE, self.t11_validate)]
        return plan

    def _execute(self, m: RunManifest) -> RunManifest:
        ctx = m.context
        self._m = m  # stage functions read gate decisions from it
        m.status = RunStatus.RUNNING
        self.store.save_manifest(m)
        try:
            for stage, fn in self._plan(ctx.mode):
                done = self.store.get_checkpoint(ctx.run_id, stage)
                if done and done.status in (StageStatus.DONE, StageStatus.SKIPPED):
                    m.stages[stage] = done
                    self._rehydrate(ctx, stage)
                    continue
                res = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=utcnow())
                self.store.event(ctx.run_id, stage.value, "stage.start")
                try:
                    out = fn(ctx)
                    res.status = StageStatus.SKIPPED if out.pop("_skipped", False) else StageStatus.DONE
                    res.output = out
                except GateRequired as g:
                    res.status, res.output = StageStatus.WAITING_GATE, {"gate": str(g)}
                    m.stages[stage] = res
                    m.status = RunStatus.WAITING_GATE
                    self.store.checkpoint(ctx.run_id, res)
                    self.store.event(ctx.run_id, stage.value, "gate.wait", gate=str(g))
                    return m
                except Exception as e:  # any error, not only AdapterError, must fail the run and free the env
                    adapter = isinstance(e, AdapterError)
                    # Masked: error text can quote raw results or logs.
                    error = self.masker.mask(str(e) if adapter else f"{type(e).__name__}: {e}")
                    res.status, res.error = StageStatus.FAILED, error
                    m.stages[stage] = res
                    m.status = RunStatus.FAILED
                    self.store.checkpoint(ctx.run_id, res)
                    self.store.event(ctx.run_id, stage.value, "stage.failed", error=error,
                                     retryable=e.retryable if adapter else False)
                    return m
                res.finished_at = utcnow()
                m.stages[stage] = res
                self.store.checkpoint(ctx.run_id, res)
                self.store.event(ctx.run_id, stage.value, "stage.done", status=res.status.value)
            m.status = RunStatus.SUCCEEDED
            return m
        finally:
            if m.status == RunStatus.RUNNING:  # an error outside a stage function (store, resume) escaped
                m.status = RunStatus.FAILED
            self.store.save_manifest(m)
            if m.status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
                self._release_env(ctx)

    def _guard(self, ctx: RunContext) -> None:
        if ctx.product != self.profile.product:
            raise ValueError(f"run is for {ctx.product!r} but profile is {self.profile.product!r}")
        if ctx.env not in self.profile.policy.environments:
            raise ValueError(f"environment {ctx.env!r} is not allowed by policy.environments")

    def _rehydrate(self, ctx: RunContext, stage: Stage) -> None:
        """Reload stage outputs needed by later stages when resuming."""
        if stage in (Stage.T6_NORMALIZE, Stage.T7_TRIAGE, Stage.T8_VERIFY):
            for suite in self._selected(ctx):
                body = self.store.get_artifact(ctx.run_id, f"atrf/{suite}.json")
                if body:
                    self._atrf[suite] = ATRFRun.model_validate_json(body)
                base = self.store.get_baseline(ctx.product, ctx.merge_base, suite)
                if base:
                    self._baseline[suite] = ATRFRun.model_validate_json(base)

    def _selected(self, ctx: RunContext) -> list[str]:
        cp = self.store.get_checkpoint(ctx.run_id, Stage.T2_SELECT)
        return cp.output["suites"] if cp else []

    # ---------------------------------------------------------------- stages
    def t1_impact(self, ctx: RunContext) -> dict[str, Any]:
        # LLM stage (skills/t01-impact). Not wired in the scaffold: selection uses rules only.
        return {"_skipped": True, "reason": "T1 impact skill not configured; rule-based selection only"}

    def t2_select(self, ctx: RunContext) -> dict[str, Any]:
        suites = select_suites(self.profile, ctx.changed_paths)
        return {"suites": suites}

    def t3_author(self, ctx: RunContext) -> dict[str, Any]:
        if ctx.mode == RunMode.REPORT_ONLY or self.profile.test_authoring is None:
            return {"_skipped": True, "reason": "test authoring disabled in this mode"}
        if approved := self._approval(Stage.T3_AUTHOR):
            return {"_skipped": True, "reason": f"test authoring is not implemented yet; gate approved by {approved.by}"}
        raise GateRequired("T3: approve proposed test changes")

    def t4_env(self, ctx: RunContext) -> dict[str, Any]:
        lock_cfg = self.profile.policy.env_lock
        if lock_cfg:
            lock = registry.create("envlock", lock_cfg.adapter, **lock_cfg.params)
            if not lock.acquire(ctx.env, ctx.run_id, lock_cfg.wait_max_min):
                raise AdapterError(f"environment {ctx.env} is held by another run; queued", retryable=True)
            self.store.event(ctx.run_id, "T4", "env.hold", env=ctx.env)
        deploy = registry.create("pipeline", self.profile.deploy.adapter, **self.profile.deploy.params)
        handle = deploy.trigger(ctx, ctx.commit)
        self.store.event(ctx.run_id, "T4", "external.call", adapter=handle.adapter, job=handle.job_ref)
        status = self._wait(lambda: deploy.poll(handle), timeout_min=60,
                            max_interval=self.profile.deploy.params.get("pollSec", DEFAULT_POLL_SEC))
        if status != ExecStatus.DONE:
            raise AdapterError(f"deploy of {ctx.commit} ended with {status.value}: env-blocked", retryable=False)
        return {"deployed": ctx.commit, "job": handle.job_ref}

    def t5_execute(self, ctx: RunContext) -> dict[str, Any]:
        out: dict[str, Any] = {"suites": {}}
        for suite in self._selected(ctx):
            cfg = self.profile.suite(suite)
            ex = registry.create("executor", cfg.executor.adapter, **cfg.executor.params)
            raw = self._run_suite(ex, suite, ctx, ctx.commit, cfg.max_duration_min)
            # Raw results are unmasked: held only until T6 parses them, then wiped. In the ATH
            # service this is an encrypted, short-TTL object rather than a state-store row.
            self.store.put_artifact(ctx.run_id, f"raw/{suite}", {k: v.decode("utf-8", "replace") for k, v in raw.files.items()})
            cached = self.store.get_baseline(ctx.product, ctx.merge_base, suite)
            if cached is None:
                base_raw = self._run_suite(ex, suite, ctx, ctx.merge_base, cfg.max_duration_min)
                base = self._parse(base_raw, ctx, suite, ctx.merge_base)
                self.store.put_baseline(ctx.product, ctx.merge_base, suite, base.model_dump_json())
                out["suites"][suite] = "baseline-run"
            else:
                out["suites"][suite] = "baseline-cached"
        return out

    def t6_normalize(self, ctx: RunContext) -> dict[str, Any]:
        out = {}
        for suite in self._selected(ctx):
            raw_json = self.store.get_artifact(ctx.run_id, f"raw/{suite}")
            files = {k: v.encode() for k, v in json.loads(raw_json).items()}
            run = self._parse(RawResults(format=self.profile.suite(suite).results.format, files=files), ctx, suite, ctx.commit)
            run.baseline_commit = ctx.merge_base
            self._atrf[suite] = run
            self._baseline[suite] = ATRFRun.model_validate_json(self.store.get_baseline(ctx.product, ctx.merge_base, suite))
            self.store.put_artifact(ctx.run_id, f"atrf/{suite}.json", run.model_dump_json())
            self.store.put_artifact(ctx.run_id, f"raw/{suite}", "{}")  # raw results never outlive T6
            out[suite] = run.summary.model_dump()
        out["masked"] = dict(self.masker.hits)
        return out

    def t7_triage(self, ctx: RunContext) -> dict[str, Any]:
        counts: dict[str, int] = {}
        diff = self.store.get_artifact(ctx.run_id, "context/diff") or ""
        tsd = self.store.get_artifact(ctx.run_id, "context/tsd") or ""
        # Parsers mask case ids, so mask the listed ids the same way (own masker: not counted as hits).
        ids = Masker(categories=self.profile.logs.masking, correlation_keys=self.profile.correlation_keys())
        known = [k.model_copy(update={"case_id": ids.mask(k.case_id)}) for k in self.profile.policy.known_failures]
        quarantined = {ids.mask(q) for q in self.quarantined}
        for suite, run in self._atrf.items():
            cfg = self.profile.suite(suite)
            ex = registry.create("executor", cfg.executor.adapter, **cfg.executor.params)
            remaining = apply_prechecks(run, self._baseline.get(suite), known, quarantined)
            if remaining:
                remaining = mark_flaky(remaining, self._passed_on_retry(ex, ctx, suite, {c.id for c in remaining}))
            for case in remaining:
                evidence = {"logs": self._logs_for(ctx, case).lines, "diff_ref": ctx.commit,
                            "diff": diff_excerpt(diff, case) if diff else "",
                            "tsd": tsd_excerpt(tsd, case) if tsd else ""}
                case.triage = vote(case, self.agents, evidence) if self.agents else None
                for agent in self.agents:  # audit: one event per model call (hashes, never content)
                    if getattr(agent, "last_call", None):
                        self.store.event(ctx.run_id, "T7", "agent.call", case=case.id, agent=agent.name,
                                         **agent.last_call)
            for c in run.failing():
                key = c.triage.category.value if c.triage else "untriaged"
                counts[key] = counts.get(key, 0) + 1
            self.store.put_artifact(ctx.run_id, f"atrf/{suite}.json", run.model_dump_json())
        return {"categories": counts}

    def t8_verify(self, ctx: RunContext) -> dict[str, Any]:
        # Untriaged failures (no agents configured) become `unknown`, i.e. human review.
        n = 0
        for suite, run in self._atrf.items():
            for c in run.failing():
                if c.triage is None:
                    c.triage = Triage(category=TriageCategory.UNKNOWN, decided_by="precheck",
                                      reason="no triage agents configured; needs human review")
                    n += 1
            self.store.put_artifact(ctx.run_id, f"atrf/{suite}.json", run.model_dump_json())
        return {"needs_review": n}

    def t9_report(self, ctx: RunContext) -> dict[str, Any]:
        notes = []
        if not self.agents:
            notes.append("LLM triage agents are not configured; unexplained failures are marked unknown.")
        report = render_report(ctx, list(self._atrf.values()), notes)
        self.store.put_artifact(ctx.run_id, "report.md", report)
        path = None
        if self.report_dir:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            path = self.report_dir / f"{ctx.run_id}.md"
            path.write_text(report, encoding="utf-8")
        # TODO: post to MR and Jira through the credential broker (user's profile credentials).
        return {"report": str(path) if path else "artifact:report.md"}

    def t10_remediate(self, ctx: RunContext) -> dict[str, Any]:
        regressions = [c.id for run in self._atrf.values() for c in run.failing()
                       if c.triage and c.triage.category == TriageCategory.CODE_REGRESSION]
        if not regressions:
            return {"_skipped": True, "reason": "no code-regression failures to remediate"}
        if approved := self._approval(Stage.T10_REMEDIATE):
            return {"_skipped": True, "cases": regressions,
                    "reason": f"remediation is not implemented yet; redeploy gate approved by {approved.by}"}
        raise GateRequired(f"T10: approve redeploy of a fix for {len(regressions)} code-regression "
                           f"failure(s): {', '.join(regressions)}")

    def t11_validate(self, ctx: RunContext) -> dict[str, Any]:
        return {"_skipped": True}

    # ---------------------------------------------------------------- helpers
    def _wait(self, poll: Callable[[], ExecStatus], timeout_min: float,
              max_interval: float = DEFAULT_POLL_SEC) -> ExecStatus:
        """Poll until the job leaves RUNNING or the timeout passes, backing off between checks."""
        deadline = self._clock() + timeout_min * 60
        interval = min(FIRST_POLL_SEC, max_interval)
        while True:
            st = poll()
            if st != ExecStatus.RUNNING:
                return st
            remaining = deadline - self._clock()
            if remaining <= 0:
                return ExecStatus.TIMEOUT
            self._sleep(min(interval, remaining))
            interval = min(interval * POLL_BACKOFF, max_interval)

    def _run_suite(self, ex, suite: str, ctx: RunContext, commit: str, max_min: int):
        handle = ex.start(suite, ctx, commit)
        self.store.event(ctx.run_id, "T5", "external.call", adapter=handle.adapter, job=handle.job_ref)
        st = self._wait(lambda: ex.poll(handle), timeout_min=max_min,
                        max_interval=self.profile.suite(suite).executor.params.get("pollSec", DEFAULT_POLL_SEC))
        if st not in (ExecStatus.DONE, ExecStatus.FAILED):
            raise AdapterError(f"suite {suite} on {commit} ended with {st.value}")
        return ex.collect(handle)

    def _parse(self, raw, ctx: RunContext, suite: str, commit: str) -> ATRFRun:
        cfg = self.profile.suite(suite)
        parser = registry.create("parser", cfg.results.parser or cfg.results.format)
        return parser.parse(raw, run_id=ctx.run_id, product=ctx.product, suite=suite,
                            env=ctx.env, commit=commit, masker=self.masker)

    def _passed_on_retry(self, ex, ctx: RunContext, suite: str, candidates: set[str]) -> set[str]:
        """Rerun the suite up to flakyRetries times; return candidate cases that passed in any rerun.

        One suite rerun covers all failing cases at once. Executors that support running
        selected cases only should use that to keep reruns short.
        """
        passed: set[str] = set()
        for attempt in range(self.profile.policy.flaky_retries):
            if not candidates - passed:
                break
            raw = self._run_suite(ex, suite, ctx, ctx.commit, self.profile.suite(suite).max_duration_min)
            for cid, case in self._parse(raw, ctx, suite, ctx.commit).by_id().items():
                if cid in candidates and not case.is_failing:
                    passed.add(cid)
            self.store.event(ctx.run_id, "T7", "flaky.retry", suite=suite, attempt=attempt + 1, passed=sorted(passed))
        return passed

    def _logs_for(self, ctx: RunContext, case: ATRFCase):
        end = datetime.now(timezone.utc)
        for src in self.profile.logs.sources:
            try:
                ls = registry.create("logs", src.adapter, **src.params)
                logs = ls.fetch(end - timedelta(minutes=15), end, case.correlation, 200, self.masker)
                self.store.event(ctx.run_id, "T7", "external.call", adapter=src.adapter, case=case.id)
                return logs
            except (AdapterError, NotImplementedError) as e:
                self.store.event(ctx.run_id, "T7", "logsource.fallback", adapter=src.adapter, error=str(e))
        return MaskedLogs(source="none", lines=[])

    def _release_env(self, ctx: RunContext) -> None:
        lock_cfg = self.profile.policy.env_lock
        if lock_cfg:
            registry.create("envlock", lock_cfg.adapter, **lock_cfg.params).release(ctx.env, ctx.run_id)
