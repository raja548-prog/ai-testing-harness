"""ATH orchestrator: runs stages in order, checkpoints each one, enforces mode and gates.

This in-process runner is the reference implementation of the run semantics. In the
ATH service the same stage functions are wrapped as workflow activities (e.g. Temporal),
which adds durable timers, webhook signals and multi-day gate waits.

Scaffold status:
- Implemented: T2 selection, T3 test authoring (local-fs: one proposal, behind its gate),
  T4 env hold + deploy, T5 execute (MR + cached merge-base baseline), T6 normalize with masking,
  T7 pre-checks + pluggable agent voting, T8 quorum, T9 report, resume from checkpoints,
  structured events.
- Stubbed: T1 impact (LLM), T10/T11 fix loop.
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
from ath_core.authoring.engine import AuthorAgent, AuthorInputs, AuthoringEngine
from ath_core.context import diff_excerpt, load_product_skill, tsd_excerpt
from ath_core.gates import approval_problem, rejection_problem
from ath_core.masking import Masker
from ath_core.report.markdown import render_report
from ath_core.schemas.atrf import ATRFCase, ATRFRun, Triage, TriageCategory
from ath_core.schemas.authoring import Proposal
from ath_core.schemas.profile import SuiteConfig, TestProfile
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
                 report_dir: str | Path | None = None, profile_ref: str | None = None,
                 quorum: int | None = None, author_factory: Callable[[], AuthorAgent] | None = None):
        self.profile = profile
        self.profile_ref = profile_ref
        self.store = store
        self.package_version = package_version
        self.agents = agents or []
        self.quorum = quorum  # None: default_quorum(len(agents))
        # Builds the T3 author when T3 first needs a model call (it checks the model policy then).
        self.author_factory = author_factory
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
        {"diff": unified diff, "tsd": TSD markdown}. It is masked and stored with the run.

        An existing run id continues that run only if `ctx` is the same; rejected runs stay stopped."""
        self.check(ctx)
        m = RunManifest(context=ctx, core_version=CORE_VERSION, package_version=self.package_version,
                        profile_hash=self._profile_hash(), profile_ref=self.profile_ref)
        masked = {name: self.masker.mask(text) for name, text in (context or {}).items()}
        m.inputs = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in masked.items()}
        if self.store.create_manifest(m):
            for name, text in masked.items():
                self.store.put_artifact(ctx.run_id, f"context/{name}", text)
        elif self._load(ctx.run_id).context != ctx:
            raise ValueError(f"run {ctx.run_id} already exists with different inputs; resume it, "
                             f"or start a new run with another run id")
        return self._execute(ctx.run_id)

    def resume(self, run_id: str, force: bool = False) -> RunManifest:
        """Continue a run from its checkpoints. `force` takes over a run left RUNNING by a process that died."""
        self._load(run_id)  # KeyError for an unknown run
        return self._execute(run_id, force=force)

    def approve(self, run_id: str, stage: Stage, user: str, groups: list[str], comment: str = "",
                ref: str | None = None) -> RunManifest:
        """Record an approval (separation of duties applies), then continue the run past the gate.
        `ref`, if given, must be the hash of what is being approved (T3: the proposal hash)."""
        self.record_gate(run_id, stage, "approved", user, groups, comment, ref=ref)
        return self._execute(run_id)

    def reject(self, run_id: str, stage: Stage, user: str, groups: list[str], comment: str = "") -> RunManifest:
        """Stop the run at a gate. Artifacts are kept; the env hold is released."""
        return self.record_gate(run_id, stage, "rejected", user, groups, comment)

    def record_gate(self, run_id: str, stage: Stage, decision: str, user: str, groups: list[str],
                    comment: str = "", ref: str | None = None) -> RunManifest:
        """Check and record a gate decision without running anything (the API resumes in the background).

        Raises PermissionError when the decider may not decide, ValueError if the profile changed or
        `ref` is not the hash of what waits for approval. A T3 approval records the proposal hash, so
        T3 applies exactly the proposal that was approved.
        Check and write are one atomic step, so concurrent decisions cannot both pass."""
        rule = approval_problem if decision == "approved" else rejection_problem
        current = None
        if stage == Stage.T3_AUTHOR:
            body = self.store.get_artifact(run_id, "author/proposal.json")
            current = Proposal.model_validate_json(body).hash if body else None
        if ref and stage != Stage.T3_AUTHOR:
            raise ValueError(f"--ref applies to the T3 gate only, not {stage.value}")
        if ref and ref != current:
            raise ValueError(f"--ref {ref[:12]}... is not the stored T3 proposal "
                             f"({current[:12] + '...' if current else 'none'}); review it again before deciding")

        def decide(m: RunManifest) -> None:
            if m.profile_hash != self._profile_hash():
                raise ValueError(f"the profile changed since run {run_id} started; decide with the profile it ran with")
            if problem := rule(m, stage, user, groups, self.profile.owners):
                raise PermissionError(problem)
            m.gates.append(GateDecision(stage=stage, decision=decision, by=user, groups=groups, comment=comment,
                                        ref=current))
            if decision == "rejected":
                res = m.stages[stage]
                res.status, res.error, res.finished_at = StageStatus.REJECTED, f"rejected by {user}: {comment}", utcnow()
                m.status = RunStatus.REJECTED

        m = self.store.update_manifest(run_id, decide)
        self.store.event(run_id, stage.value, f"gate.{decision}", by=user, groups=groups, comment=comment,
                         **({"ref": current} if current else {}))
        if decision == "rejected":
            self.store.checkpoint(run_id, m.stages[stage])
            self._release_env(m.context)
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

    def _claim(self, run_id: str, force: bool) -> tuple[RunManifest, RunStatus]:
        """Mark the run RUNNING, atomically, so two processes never execute it at once."""
        before: list[RunStatus] = []

        def claim(m: RunManifest) -> None:
            if m.status == RunStatus.REJECTED:
                raise ValueError(f"run {run_id} was rejected at a gate and cannot be resumed")
            if m.status == RunStatus.RUNNING and not force:
                raise ValueError(f"run {run_id} is already running; if its process died, resume it with --force")
            before.append(m.status)
            m.status = RunStatus.RUNNING

        m = self.store.update_manifest(run_id, claim)
        return m, before[0]

    def _execute(self, run_id: str, force: bool = False) -> RunManifest:
        m, before = self._claim(run_id, force)
        ctx = m.context
        self._m = m  # stage functions read gate decisions from it
        self._atrf, self._baseline = {}, {}  # per-run state: an orchestrator may run several runs
        self.masker.hits.clear()
        plan = self._plan(ctx.mode)
        done = {s: cp for s, _ in plan
                if (cp := self.store.get_checkpoint(ctx.run_id, s)) and cp.status in (StageStatus.DONE, StageStatus.SKIPPED)}
        # A failed (or crashed) run gave up its env hold, and another run may have deployed since:
        # if stages that use the env are left, take the hold again and redeploy.
        uses_env = {Stage.T5_EXECUTE, Stage.T7_TRIAGE, Stage.T10_REMEDIATE, Stage.T11_VALIDATE}
        env_left = any(s in uses_env and s not in done for s, _ in plan)
        if before in (RunStatus.FAILED, RunStatus.RUNNING) and Stage.T4_ENV in done and env_left:
            del done[Stage.T4_ENV]
            self.store.event(ctx.run_id, "T4", "env.redeploy", reason=f"resuming a {before.value} run")
        try:
            for stage, fn in plan:
                if stage in done:
                    m.stages[stage] = done[stage]
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

    def check(self, ctx: RunContext) -> None:
        """Refuse a run this profile does not allow (wrong product, environment not in policy)."""
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
                if (base := self._load_baseline(ctx, suite)) is not None:
                    self._baseline[suite] = base

    def _selected(self, ctx: RunContext) -> list[str]:
        cp = self.store.get_checkpoint(ctx.run_id, Stage.T2_SELECT)
        return cp.output["suites"] if cp else []

    def _incomplete(self, ctx: RunContext) -> list[str]:
        """Suites whose MR run stopped early in T5, so their results may be partial."""
        cp = self.store.get_checkpoint(ctx.run_id, Stage.T5_EXECUTE)
        return cp.output.get("incomplete", []) if cp else []

    def _load_baseline(self, ctx: RunContext, suite: str) -> ATRFRun | None:
        body = self.store.get_baseline(ctx.product, ctx.merge_base, suite)
        return ATRFRun.model_validate_json(body) if body else None

    # ---------------------------------------------------------------- stages
    def t1_impact(self, ctx: RunContext) -> dict[str, Any]:
        # LLM stage (skills/t01-impact). Not wired in the scaffold: selection uses rules only.
        return {"_skipped": True, "reason": "T1 impact skill not configured; rule-based selection only"}

    def t2_select(self, ctx: RunContext) -> dict[str, Any]:
        suites = select_suites(self.profile, ctx.changed_paths)
        return {"suites": suites}

    def _t3_skip(self, ctx: RunContext) -> str | None:
        """Why T3 does not run in this run, or None."""
        ta = self.profile.test_authoring
        if ctx.mode == RunMode.REPORT_ONLY:
            return "test authoring is off in report-only mode"
        if ta is None:
            return "the profile has no testAuthoring section"
        if not (ctx.author if ctx.author is not None else ta.enabled):
            return ("turned off for this run (--no-author)" if ctx.author is False
                    else "turned off in the profile (testAuthoring.enabled: false)")
        if ta.mode != "local-fs":
            return f"mode {ta.mode} is not implemented yet"
        if not ta.sources:
            return "testAuthoring.sources is empty"
        if not self.store.get_artifact(ctx.run_id, "context/tsd"):
            return "no TSD given (--tsd or --rh-package)"
        if not ctx.tests_dir:
            return "no test checkout given (--tests)"
        return None

    def t3_author(self, ctx: RunContext) -> dict[str, Any]:
        if reason := self._t3_skip(ctx):
            return {"_skipped": True, "reason": reason}
        # The proposal is made once: a resume, or a crash after the model call, reuses the stored one.
        body = self.store.get_artifact(ctx.run_id, "author/proposal.json")
        if body is None:
            if self.author_factory is None:
                return {"_skipped": True, "reason": "no agent runner configured for LLM stages"}
            agent = self.author_factory()  # ValueError (e.g. the model policy) fails T3 before any call
            code_dir = Path(ctx.code_dir) if ctx.code_dir else None
            inputs = AuthorInputs(tsd=self.store.get_artifact(ctx.run_id, "context/tsd") or "",
                                  tests_dir=Path(ctx.tests_dir), code_dir=code_dir,
                                  diff=self.store.get_artifact(ctx.run_id, "context/diff"))
            try:
                drafted = AuthoringEngine(self.profile, agent).propose(inputs, source=f"run:{ctx.run_id}",
                                                                       proposal_id=f"{ctx.run_id}-t3")
            finally:  # audit: hashes, never content; failed calls too
                if getattr(agent, "last_call", None):
                    self.store.event(ctx.run_id, "T3", "agent.call", agent="cli-author", **agent.last_call)
            proposal = drafted.proposal
            self.store.put_artifact(ctx.run_id, "author/proposal.json", proposal.canonical_json())
            self.store.put_artifact(ctx.run_id, "author/changes.patch", drafted.patch)
            self.store.put_artifact(ctx.run_id, "author/checklist.md", drafted.checklist)
            self.store.event(ctx.run_id, "T3", "author.proposed", proposal=proposal.hash,
                             changes=len(proposal.changes), dropped=len(proposal.dropped),
                             spec_questions=len(proposal.spec_questions))
        else:
            proposal = Proposal.model_validate_json(body)
        h = proposal.hash
        out: dict[str, Any] = {"proposal": h, "changes": [c.path for c in proposal.changes],
                               "dropped": len(proposal.dropped), "spec_questions": len(proposal.spec_questions)}
        if not proposal.changes:
            return {**out, "applied": [], "reason": "no proposed change passed the guards; nothing to approve"}
        approved = self._approval(Stage.T3_AUTHOR)
        if approved is None:
            raise GateRequired(f"T3: approve {len(proposal.changes)} proposed test change(s); proposal {h} "
                               f"(review artifacts author/changes.patch and author/checklist.md)")
        if approved.ref != h:
            raise ValueError(f"proposal changed after approval: approved {str(approved.ref)[:12]}, stored {h[:12]}")
        adapter = registry.create("author", self.profile.test_authoring.mode, profile=self.profile)
        result = adapter.apply(proposal, Path(ctx.tests_dir), approved.by)
        self.store.event(ctx.run_id, "T3", "author.applied", proposal=h, applied=result.applied,
                         already_applied=result.already_applied, by=approved.by)
        return {**out, "applied": result.applied, "already_applied": result.already_applied,
                "approved_by": approved.by}

    def t4_env(self, ctx: RunContext) -> dict[str, Any]:
        lock_cfg = self.profile.policy.env_lock
        if lock_cfg:
            lock = registry.create("envlock", lock_cfg.adapter, **lock_cfg.params)
            if not lock.acquire(ctx.env, ctx.run_id, lock_cfg.wait_max_min):
                raise AdapterError(f"environment {ctx.env} is held by another run; queued", retryable=True)
            self.store.event(ctx.run_id, "T4", "env.hold", env=ctx.env)
        deploy = registry.create("pipeline", self.profile.deploy.adapter, **self.profile.deploy.params)
        redeploys = sum(k == "env.redeploy" for _, k, _ in self.store.events(ctx.run_id))
        handle = deploy.trigger(ctx, ctx.commit, attempt=redeploys)  # a redeploy must not reuse the old job
        self.store.event(ctx.run_id, "T4", "external.call", adapter=handle.adapter, job=handle.job_ref)
        status = self._wait(lambda: deploy.poll(handle), timeout_min=60,
                            max_interval=self.profile.deploy.poll_sec or DEFAULT_POLL_SEC)
        if status != ExecStatus.DONE:
            raise AdapterError(f"deploy of {ctx.commit} ended with {status.value}: env-blocked", retryable=False)
        return {"deployed": ctx.commit, "job": handle.job_ref}

    def t5_execute(self, ctx: RunContext) -> dict[str, Any]:
        out: dict[str, Any] = {"suites": {}, "incomplete": []}
        for suite in self._selected(ctx):
            cfg = self.profile.suite(suite)
            ex = registry.create("executor", cfg.executor.adapter, **cfg.executor.params)
            raw, complete = self._run_suite(ex, cfg, ctx, ctx.commit)
            if not complete:
                out["incomplete"].append(suite)
            # Raw results are unmasked: held only until T6 parses them, then wiped. In the ATH
            # service this is an encrypted, short-TTL object rather than a state-store row.
            self.store.put_artifact(ctx.run_id, f"raw/{suite}", {k: v.decode("utf-8", "replace") for k, v in raw.files.items()})
            cached = self.store.get_baseline(ctx.product, ctx.merge_base, suite)
            if cached is None:
                base_raw, base_complete = self._run_suite(ex, cfg, ctx, ctx.merge_base)
                if base_complete:
                    base = self._parse(base_raw, ctx, suite, ctx.merge_base)
                    self.store.put_baseline(ctx.product, ctx.merge_base, suite, base.model_dump_json())
                    out["suites"][suite] = "baseline-run"
                else:  # never cache a partial baseline; without one, no failure is called pre-existing
                    out["suites"][suite] = "baseline-incomplete"
            else:
                out["suites"][suite] = "baseline-cached"
        return out

    def t6_normalize(self, ctx: RunContext) -> dict[str, Any]:
        out = {}
        selected = self._selected(ctx)
        for suite in selected:
            raw_json = self.store.get_artifact(ctx.run_id, f"raw/{suite}")
            parsed = self.store.get_artifact(ctx.run_id, f"atrf/{suite}.json")
            if raw_json in (None, "{}") and parsed:
                run = ATRFRun.model_validate_json(parsed)  # parsed by an earlier attempt, raw results already wiped
            else:
                files = {k: v.encode() for k, v in json.loads(raw_json or "{}").items()}
                run = self._parse(RawResults(format=self.profile.suite(suite).results.format, files=files),
                                  ctx, suite, ctx.commit)
                run.baseline_commit = ctx.merge_base
                run.incomplete = suite in self._incomplete(ctx)
                self.store.put_artifact(ctx.run_id, f"atrf/{suite}.json", run.model_dump_json())
            self._atrf[suite] = run
            self._baseline[suite] = self._load_baseline(ctx, suite)
            out[suite] = run.summary.model_dump()
        # Raw results never outlive T6. Wiped only once every suite is parsed, so a resume after a
        # failure part-way still has them.
        for suite in selected:
            self.store.put_artifact(ctx.run_id, f"raw/{suite}", "{}")
        out["masked"] = dict(self.masker.hits)
        return out

    def t7_triage(self, ctx: RunContext) -> dict[str, Any]:
        counts: dict[str, int] = {}
        diff = self.store.get_artifact(ctx.run_id, "context/diff") or ""
        tsd = self.store.get_artifact(ctx.run_id, "context/tsd") or ""
        product = self._product_skill(ctx)
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
                            "tsd": tsd_excerpt(tsd, case) if tsd else "", "product": product}
                case.triage = vote(case, self.agents, evidence, self.quorum) if self.agents else None
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
        report = render_report(ctx, list(self._atrf.values()), notes, authoring=self._authoring_summary(ctx))
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
    def _product_skill(self, ctx: RunContext) -> str:
        """The masked product skill for LLM evidence; "" when not configured or unreadable (recorded)."""
        try:
            skill = load_product_skill(self.profile)
        except OSError as e:
            self.store.event(ctx.run_id, "T7", "context.missing", input="productSkill", error=str(e))
            return ""
        return self.masker.mask(skill.text) if skill else ""

    def _authoring_summary(self, ctx: RunContext) -> dict[str, Any] | None:
        """What T3 did, for the report; None when T3 was skipped because it does not apply to the run."""
        res = self._m.stages.get(Stage.T3_AUTHOR)
        if res is None:
            return None
        body = self.store.get_artifact(ctx.run_id, "author/proposal.json")
        return {"status": res.status.value, "reason": res.output.get("reason", ""),
                "applied": res.output.get("applied", []), "approved_by": res.output.get("approved_by"),
                "proposal": Proposal.model_validate_json(body) if body else None}

    def _wait(self, poll: Callable[[], ExecStatus], timeout_min: float,
              max_interval: float = DEFAULT_POLL_SEC) -> ExecStatus:
        """Poll until the job leaves RUNNING or the timeout passes, backing off between checks."""
        deadline = self._clock() + timeout_min * 60
        max_interval = max(max_interval, 1.0)  # pollSec is validated, but a never-sleeping loop must not happen
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

    def _run_suite(self, ex, cfg: SuiteConfig, ctx: RunContext, commit: str,
                   attempt: int = 0) -> tuple[RawResults, bool]:
        """Run one suite to the end. Returns its raw results and whether the job ran to completion."""
        handle = ex.start(cfg.id, ctx, commit, attempt=attempt)
        self.store.event(ctx.run_id, "T5", "external.call", adapter=handle.adapter, job=handle.job_ref)
        st = self._wait(lambda: ex.poll(handle), timeout_min=cfg.max_duration_min,
                        max_interval=cfg.executor.poll_sec or DEFAULT_POLL_SEC)
        if st not in (ExecStatus.DONE, ExecStatus.FAILED):
            raise AdapterError(f"suite {cfg.id} on {commit} ended with {st.value}")
        return ex.collect(handle), ex.complete(handle)

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
            raw, _ = self._run_suite(ex, self.profile.suite(suite), ctx, ctx.commit, attempt=attempt + 1)
            for cid, case in self._parse(raw, ctx, suite, ctx.commit).by_id().items():
                if cid in candidates and not case.is_failing:
                    passed.add(cid)
            self.store.event(ctx.run_id, "T7", "flaky.retry", suite=suite, attempt=attempt + 1, passed=sorted(passed))
        return passed

    def _logs_for(self, ctx: RunContext, case: ATRFCase):
        end = datetime.now(timezone.utc)
        for src in self.profile.logs.sources:
            try:
                ls = registry.create("logs", src.adapter, **{"product": self.profile.product, **src.params})
                logs = ls.fetch(end - timedelta(minutes=15), end, case.correlation, 200, self.masker)
                self.store.event(ctx.run_id, "T7", "external.call", adapter=src.adapter, case=case.id)
                return logs
            except Exception as e:  # any log source problem: try the next one, never fail triage over logs
                self.store.event(ctx.run_id, "T7", "logsource.fallback", adapter=src.adapter,
                                 error=self.masker.mask(f"{type(e).__name__}: {e}"))
        return MaskedLogs(source="none", lines=[])

    def _release_env(self, ctx: RunContext) -> None:
        lock_cfg = self.profile.policy.env_lock
        if lock_cfg:
            registry.create("envlock", lock_cfg.adapter, **lock_cfg.params).release(ctx.env, ctx.run_id)
