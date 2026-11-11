"""`ainxt-ath` CLI. Ships as the `ainxt ath ...` plugin of the AiNxt CLI.

Local development runs everything in-process against a SQLite state store. In the
platform, the same commands call the ATH API with the user's SSO token instead.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

from ath_core.adapters import AdapterError, registry
from ath_core.adapters import loader  # noqa: F401
from ath_core.agents.cli import (
    AgentError, author_model, build_author_agent, build_triage_agents, diversity_problem, load_skill, model_family,
    model_problem, voter_models,
)
from ath_core.authoring.engine import AuthorInputs, AuthoringEngine, load_proposal, save_proposal
from ath_core.authoring.formats import glob_match
from ath_core.authoring.testdata import load_catalogue
from ath_core.triage.voting import default_quorum
from ath_core.config import load_config
from ath_core.context import load_product_skill, read_rh_package
from ath_core.policy import load_policy
from ath_core.schemas import ATRFRun, RunContext, RunMode, Stage, TriageCategory, load_profile
from ath_core.schemas.authoring import Proposal
from ath_server.orchestrator import Orchestrator, StateStore

KIND_FOR = {"build": "pipeline", "deploy": "pipeline"}


def cmd_validate(a: argparse.Namespace) -> int:
    try:
        p = load_profile(a.profile)
    except Exception as e:  # pydantic ValidationError or ValueError
        print(f"INVALID: {e}")
        return 1
    print(f"OK: {p.product} ({len(p.suites)} suites, environments {p.policy.environments})")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    if cmd_validate(a):
        return 1
    p = load_profile(a.profile)
    checks: list[tuple[str, str, str, dict]] = [
        ("pipeline", p.build.adapter, "build", p.build.params),
        ("pipeline", p.deploy.adapter, "deploy", p.deploy.params),
    ]
    checks += [("executor", s.executor.adapter, f"suite {s.id}", s.executor.params) for s in p.suites]
    checks += [("parser", s.results.parser or s.results.format, f"suite {s.id} results", {}) for s in p.suites]
    checks += [("logs", s.adapter, "log source", s.params) for s in p.logs.sources]
    if p.policy.env_lock:
        checks.append(("envlock", p.policy.env_lock.adapter, "env lock", p.policy.env_lock.params))
    bad = 0
    for kind, name, where, params in checks:
        if name not in registry.names(kind):
            bad += 1
            print(f"  [MISSING] {where}: {kind} adapter '{name}'")
            continue
        # Adapters may report configuration problems (missing params, unset secrets).
        problems = []
        if kind != "parser" and hasattr(registry.get(kind, name), "config_problems"):
            try:
                problems = registry.create(kind, name, **params).config_problems()
            except Exception as e:  # e.g. a parameter the adapter does not accept
                problems = [f"adapter could not be created: {type(e).__name__}: {e}"]
        bad += bool(problems)
        print(f"  [{'ok' if not problems else 'CONFIG'}] {where}: {kind} adapter '{name}'")
        for problem in problems:
            print(f"      - {problem}")
    unused = [f for f, on in (("deploy.healthCheck", p.deploy.health_check), ("policy.quarantineList",
              p.policy.quarantine_list)) if on]
    for field in unused:  # a warning, not a failure: they are valid, just not acted on yet
        print(f"  [note] {field} is set but runs do not use it yet; use `--quarantine FILE` for quarantined cases"
              if field == "policy.quarantineList" else f"  [note] {field} is set but runs do not check it yet")
    bad += _doctor_agent_runner(a)
    bad += _doctor_model_policy(a, p)
    bad += _doctor_authoring(a, p)
    # TODO (platform): check the user's Git/Jira profile tokens and env reachability.
    print("doctor: all adapters available and configured" if not bad else f"doctor: {bad} adapter(s) missing or misconfigured")
    return 0 if not bad else 2


def _doctor_agent_runner(a: argparse.Namespace) -> int:
    """Report the agent CLI used for LLM stages. Returns 1 if it is configured but unusable."""
    try:
        cfg = load_config(a.config)
    except Exception as e:  # missing file, YAML or validation error
        print(f"  [CONFIG] agent runner: {e}")
        return 1
    rc = cfg.agent_runner
    if rc is None:
        print("  [--] agent runner: not configured; LLM triage is off (failures go to human review)")
        return 0
    problems = []
    if shutil.which(rc.command[0]) is None:
        problems.append(f"command {rc.command[0]!r} not found on PATH")
    try:
        load_skill(cfg.skills_dir / "t07-triage" / "SKILL.md")
    except OSError as e:
        problems.append(f"triage skill not readable: {e}")
    profile = load_profile(a.profile)
    models = voter_models(cfg, profile)
    if problem := diversity_problem(cfg, models):
        problems.append(problem)
    if problem := model_problem(cfg):
        problems.append(problem)
    try:
        if problem := load_policy(cfg.model_policy).problem(models, "triage", profile.product):
            problems.append(problem)
    except ValueError as e:
        problems.append(str(e))
    print(f"  [{'ok' if not problems else 'CONFIG'}] agent runner: {rc.command[0]} x{rc.voters} voters, "
          f"{rc.quorum or default_quorum(rc.voters)} must agree (from {cfg.source})")
    families = [model_family(m, rc.model_families) or "unclassified" for m in models]
    labels = [f"{m or '(no model)'} ({f})" for m, f in zip(models, families)]
    print(f"      voters: {', '.join(labels)}")
    if len(set(families)) == 1 and rc.voters > 1:
        print("      note: all voters share one model family, so their votes are correlated; "
              "see minFamilies in examples/ath-config.yaml")
    for problem in problems:
        print(f"      - {problem}")
    return 1 if problems else 0


def _doctor_model_policy(a: argparse.Namespace, profile) -> int:
    """Check the profile's per-stage models against the model policy. Returns 1 on a violation."""
    try:
        policy = load_policy(load_config(a.config).model_policy)
    except Exception as e:  # missing or invalid policy file
        print(f"  [CONFIG] model policy: {e}")
        return 1
    problems = []
    if profile.models:
        for stage in sorted(policy.stages):
            if problem := policy.problem([profile.models.for_stage(stage)], stage, profile.product):
                problems.append(problem)
    print(f"  [{'ok' if not problems else 'CONFIG'}] model policy: {policy.source}")
    for problem in problems:
        print(f"      - {problem}")
    return 1 if problems else 0


def _doctor_authoring(a: argparse.Namespace, p) -> int:
    """Product skill and T3 test authoring. Returns the number of problems; warnings do not count."""
    bad = 0
    if p.product_skill:
        try:
            skill = load_product_skill(p)
            problem = None if skill.meta else "has no front matter (name, product, version, approved_by)"
        except OSError as e:
            problem = f"not readable: {e}"
        bad += bool(problem)
        print(f"  [{'ok' if not problem else 'CONFIG'}] product skill: {p.resolve_path(p.product_skill)}")
        if problem:
            print(f"      - {problem}")
    ta = p.test_authoring
    if ta is None or not ta.enabled:
        print(f"  [--] test authoring (T3): {'not configured' if ta is None else 'off (enabled: false)'}")
        return bad
    problems, notes = [], []
    try:
        cfg = load_config(a.config)
        skill = load_skill(cfg.skills_dir / "t03-author" / "SKILL.md")
        if not skill.output_schema:
            problems.append("author skill has no output_schema")
        if problem := load_policy(cfg.model_policy).problem([author_model(p)], "author", p.product):
            problems.append(problem)
    except (OSError, ValueError) as e:
        problems.append(f"author skill or model policy: {e}")
    if ta.mode != "local-fs":
        notes.append(f"mode {ta.mode} is not implemented yet; runs skip T3 (use mode: local-fs)")
    if not ta.sources:
        notes.append("sources is empty: T3 cannot change any file")
    if ta.conventions and not p.resolve_path(ta.conventions).is_file():
        problems.append(f"conventions file {p.resolve_path(ta.conventions)} not found")
    tests = Path(a.tests) if getattr(a, "tests", None) else None
    if tests and tests.is_dir():
        files = _all_files(tests)
        for src in ta.sources:
            notes += [f"sources glob {g!r} matches no file in {tests}" for g in src.paths
                      if not any(glob_match(f, g) for f in files)]
        notes += [f"templates glob {g!r} matches no file in {tests}" for g in ta.templates
                  if not any(glob_match(f, g) for f in files)]
        if ta.test_data:
            try:
                n = len(load_catalogue(tests / ta.test_data).entries)
                notes.append(f"testData catalogue: {n} entries")
            except (OSError, ValueError, yaml.YAMLError) as e:
                problems.append(f"testData catalogue: {e}")
    elif tests:
        problems.append(f"--tests {tests} is not a folder")
    else:
        notes.append("no --tests checkout given: sources/templates globs and the testData catalogue were not checked")
    code = Path(a.code) if getattr(a, "code", None) else None
    if code and code.is_dir() and ta.code_context:
        files = _all_files(code)
        notes += [f"codeContext glob {g!r} matches no file in {code}" for g in ta.code_context
                  if not any(glob_match(f, g) for f in files)]
    print(f"  [{'ok' if not problems else 'CONFIG'}] test authoring (T3): mode {ta.mode}, "
          f"{sum(len(s.paths) for s in ta.sources)} source glob(s), model {author_model(p) or '(none)'}")
    for problem in problems:
        print(f"      - {problem}")
    for note in notes:
        print(f"      note: {note}")
    return bad + bool(problems)


def _all_files(root: Path) -> list[str]:
    return sorted(f.relative_to(root).as_posix() for f in root.rglob("*")
                  if f.is_file() and not any(part.startswith(".") for part in f.relative_to(root).parts))


def _orch(a: argparse.Namespace) -> Orchestrator:
    profile = load_profile(a.profile)
    cfg = load_config(a.config)
    agents = build_triage_agents(cfg, profile)
    store = StateStore(a.db)
    quarantined = set()
    if getattr(a, "quarantine", None):
        quarantined = {l.strip() for l in Path(a.quarantine).read_text().splitlines() if l.strip()}
    return Orchestrator(profile, store, package_version=a.package_version, quarantined=quarantined,
                        report_dir=a.report_dir, agents=agents, profile_ref=str(a.profile), quorum=cfg.quorum,
                        author_factory=lambda: build_author_agent(cfg, profile))


def _context(a: argparse.Namespace) -> dict[str, str]:
    """Change context for LLM stages from --diff, --tsd and --rh-package."""
    ctx: dict[str, str] = {}
    if a.diff:
        ctx["diff"] = Path(a.diff).read_text(encoding="utf-8")
    tsd = [Path(a.tsd).read_text(encoding="utf-8")] if a.tsd else []
    if a.rh_package:
        tsd.append(read_rh_package(a.rh_package))
    if tsd:
        ctx["tsd"] = "\n\n".join(tsd)
    return ctx


def _folder(path: str | None, flag: str) -> str | None:
    """An absolute folder path for the run context; ValueError if it is not a folder."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"{flag} {path} is not a folder")
    return str(p.resolve())


def _print_result(m) -> int:
    print(f"run {m.context.run_id}: {m.status.value}")
    for st, res in m.stages.items():
        extra = f"  ({res.error})" if res.error else ""
        print(f"  {st.value:<4} {res.status.value:<12}{extra}")
    return 0 if m.status.value in ("succeeded", "waiting-gate") else 1


# What a bad profile, config, quarantine file or input looks like before a run starts.
REFUSALS = (ValueError, KeyError, OSError, yaml.YAMLError)


def _why(e: Exception) -> str:
    # str(KeyError) adds quotes, and an OSError's first arg is its errno: use each one's message.
    return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)


def cmd_run(a: argparse.Namespace) -> int:
    try:
        orch = _orch(a)
        ctx = RunContext(
            run_id=a.run_id or f"ath-{uuid.uuid4().hex[:10]}", product=orch.profile.product,
            mode=RunMode(a.mode), mr=a.mr, jira=a.jira, commit=a.commit, merge_base=a.merge_base,
            env=a.env, changed_paths=a.changed or [], triggered_by=a.user, trigger_source="cli",
            author=a.author, tests_dir=_folder(a.tests, "--tests"), code_dir=_folder(a.code, "--code"),
        )
    except REFUSALS as e:  # missing or invalid file, the model-family rule, a bad --run-id
        print(f"refused: {e}")
        return 1
    try:
        m = orch.start(ctx, context=_context(a))
    except (ValueError, OSError, AdapterError) as e:
        print(f"refused: {e}")
        return 1
    rc = _print_result(m)
    report = orch.store.get_artifact(ctx.run_id, "report.md")
    if report and not a.quiet:
        print("\n" + report)
    return rc


def cmd_resume(a: argparse.Namespace) -> int:
    try:
        return _print_result(_orch(a).resume(a.run_id, force=a.force))
    except REFUSALS as e:  # unknown, rejected or running run, or a file/config problem
        print(f"refused: {_why(e)}")
        return 1


def cmd_gate(a: argparse.Namespace) -> int:
    """approve / reject a HITL gate. --groups stands in for the user's SSO groups."""
    try:
        orch = _orch(a)
        stage = Stage(a.gate)
        groups = [g.strip() for g in (a.groups or "").split(",") if g.strip()]
        if a.decision == "approved":
            m = orch.approve(a.run_id, stage, a.user, groups, a.comment or "", ref=a.ref)
        else:
            m = orch.reject(a.run_id, stage, a.user, groups, a.comment or "")
    except (*REFUSALS, PermissionError) as e:
        print(f"refused: {_why(e)}")
        return 1
    if a.decision == "rejected":
        print(f"run {a.run_id}: rejected at {stage.value} by {a.user}")
        return 0
    return _print_result(m)


def cmd_report(a: argparse.Namespace) -> int:
    body = StateStore(a.db).get_artifact(a.run_id, "report.md")
    print(body or f"no report for {a.run_id}")
    return 0 if body else 1


def cmd_eval(a: argparse.Namespace) -> int:
    """Replay a labelled dataset through the configured triage voters and gate on the scores."""
    from ath_core.evals import triage as ev  # only needed here

    try:
        profile = load_profile(a.profile)
        cfg = load_config(a.config)
        agents = build_triage_agents(cfg, profile)
        dataset = ev.load_dataset(a.dataset)
    except (ValueError, OSError) as e:
        print(f"refused: {e}")
        return 1
    if not agents:
        print("refused: no agent runner configured; set agentRunner in the harness config (see examples/ath-config.yaml)")
        return 1
    print(f"evaluating {len(dataset)} cases with {len(agents)} voters ({', '.join(a_.model for a_ in agents)})")
    def progress(i: int, n: int, o) -> None:
        mark = "ok   " if o.correct else "WRONG"
        print(f"  [{i:>2}/{n}] {mark} {o.id}: {o.predicted:<15} truth {o.truth:<15} votes {', '.join(o.votes)}",
              flush=True)

    outcomes = ev.run_eval(dataset, agents, quorum=cfg.quorum, progress=progress)
    s = ev.score(outcomes)
    report = ev.render_md(s, outcomes, title=f"Triage eval: {Path(a.dataset).name}")
    out = Path(a.out or f".ath/evals/triage-{uuid.uuid4().hex[:8]}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "results.json").write_text(json.dumps(ev.to_json(s, outcomes), indent=1), encoding="utf-8")
    print(report)
    print(f"results: {out}")
    failures = ev.threshold_failures(s, a.min_accuracy, a.max_missed_regressions, a.max_error_rate)
    for f in failures:
        print(f"GATE FAILED: {f}")
    return 3 if failures else 0


# `ainxt-ath author` exit codes (2 and 3 are taken by doctor and eval).
AUTHOR_OK, AUTHOR_REFUSED, AUTHOR_ONLY_QUESTIONS, AUTHOR_ALL_DROPPED = 0, 1, 4, 5


def _git_dirty(root: Path, paths: list[str]) -> list[str] | None:
    """`git status --porcelain` lines for these paths; None when root is not in a Git work tree."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                              capture_output=True, text=True)
    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:  # no git installed
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    if not paths:
        return []
    return [line for line in git("status", "--porcelain", "--", *paths).stdout.splitlines() if line.strip()]


def _from_run(store: StateStore, run_id: str, product: str) -> tuple[str, str, list[dict[str, str]]]:
    """(TSD, diff, test-outdated cases) stored with a run. Raises KeyError for an unknown run."""
    m = store.load_manifest(run_id)
    if m is None:
        raise KeyError(f"unknown run {run_id} in {store.db_path}")
    if m.context.product != product:
        raise ValueError(f"run {run_id} is for {m.context.product!r}, not {product!r}")
    cp = store.get_checkpoint(run_id, Stage.T2_SELECT)
    outdated = []
    for suite in (cp.output.get("suites", []) if cp else []):
        body = store.get_artifact(run_id, f"atrf/{suite}.json")
        if not body:
            continue
        for c in ATRFRun.model_validate_json(body).failing():
            if c.triage and c.triage.category == TriageCategory.TEST_OUTDATED:
                outdated.append({"id": c.id, "suite": c.suite, "name": c.name,
                                 "message": c.failure.message if c.failure else ""})
    return store.get_artifact(run_id, "context/tsd") or "", store.get_artifact(run_id, "context/diff") or "", outdated


def _print_proposal(p: Proposal, folder: Path) -> None:
    print(f"proposal {p.id}: {len(p.changes)} change(s), {len(p.dropped)} dropped, "
          f"{len(p.spec_questions)} spec question(s)")
    print(f"  hash {p.hash}")
    for c in p.changes:
        print(f"  {c.action:<6} {c.path}  [{', '.join(c.clauses)}]")
        for flag in c.flags:
            print(f"         review: {flag}")
    for d in p.dropped:
        print(f"  DROPPED {d.path}: {d.reason}")
    for q in p.spec_questions:
        print(f"  QUESTION {('[' + q.clause + '] ') if q.clause else ''}{q.question}")
    if p.summary:
        print(f"  summary: {' '.join(p.summary.split())}")
    print(f"  review: {folder / 'changes.patch'} and {folder / 'checklist.md'}")


def _author_apply(profile, p: Proposal, tests: Path, force: bool, user: str, store: StateStore) -> int:
    paths = [c.path for c in p.changes]
    dirty = _git_dirty(tests, paths)
    if not force:
        if dirty is None:
            print(f"refused: {tests} is not a Git repository, so uncommitted changes cannot be checked; "
                  f"pass --force to write anyway")
            return AUTHOR_REFUSED
        if dirty:
            print("refused: target files have uncommitted changes (commit or stash them, or pass --force):")
            for line in dirty:
                print(f"  {line}")
            return AUTHOR_REFUSED
    try:
        result = registry.create("author", "local-fs", profile=profile).apply(p, tests, user)
    except AdapterError as e:
        print(f"refused: {e}")
        return AUTHOR_REFUSED
    store.event(p.id, "T3", "author.applied", proposal=p.hash, applied=result.applied,
                already_applied=result.already_applied, by=user)
    for path in result.applied:
        print(f"  wrote {path}")
    for path in result.already_applied:
        print(f"  already applied {path}")
    print(f"applied to {tests}; review with `git diff`, then commit or open an MR as usual")
    return AUTHOR_OK


def cmd_author(a: argparse.Namespace) -> int:
    """Create or update tests from a TSD in a local checkout, without a run. `author apply <id>` applies a
    stored proposal (e.g. a --dry-run one) without a model call."""
    out_root = Path(a.out or ".ath/authoring")
    store = StateStore(a.db)
    if a.action is None and a.proposal_id:
        print(f"refused: unexpected argument {a.proposal_id!r}; did you mean `author apply {a.proposal_id}`?")
        return AUTHOR_REFUSED
    try:
        profile = load_profile(a.profile)
        ta = profile.test_authoring
        if ta is None:
            raise ValueError(f"profile {a.profile} has no testAuthoring section")
        if not ta.sources:
            raise ValueError("testAuthoring.sources is empty: list the test files T3 may change")
        tests = Path(a.tests).expanduser()
        if not tests.is_dir():
            raise ValueError(f"--tests {a.tests} is not a folder")
    except REFUSALS as e:
        print(f"refused: {_why(e)}")
        return AUTHOR_REFUSED
    if a.action == "apply":
        if not a.proposal_id:
            print("refused: give the proposal id: ainxt-ath author apply <id>")
            return AUTHOR_REFUSED
        folder = out_root / a.proposal_id
        try:
            p = load_proposal(folder)
        except (OSError, ValueError) as e:
            print(f"refused: {_why(e)}")
            return AUTHOR_REFUSED
        if p.product != profile.product:
            print(f"refused: proposal {p.id} is for {p.product!r}, not {profile.product!r}")
            return AUTHOR_REFUSED
        print(f"applying proposal {p.id} (hash {p.hash}), no model call")
        return _author_apply(profile, p, tests, a.force, a.user, store)

    agent = None
    pid = f"author-{uuid.uuid4().hex[:10]}"
    try:
        if not a.dry_run and not a.force and _git_dirty(tests, []) is None:
            raise ValueError(f"{tests} is not a Git repository, so uncommitted changes cannot be checked; "
                             f"pass --force to write anyway, or --dry-run to only propose")
        tsd, diff, outdated = "", "", []
        if a.from_run:
            tsd, diff, outdated = _from_run(store, a.from_run, profile.product)
        if a.tsd or a.rh_package:
            parts = [Path(a.tsd).read_text(encoding="utf-8")] if a.tsd else []
            if a.rh_package:
                parts.append(read_rh_package(a.rh_package))
            tsd = "\n\n".join(parts)
        if a.diff:
            diff = Path(a.diff).read_text(encoding="utf-8")
        if not tsd.strip():
            raise ValueError("no TSD: give --tsd, --rh-package, or --from-run with a run that has one")
        agent = build_author_agent(load_config(a.config), profile)
        inputs = AuthorInputs(tsd=tsd, tests_dir=tests, sections=a.section or [],
                              code_dir=Path(a.code).expanduser() if a.code else None, diff=diff or None,
                              outdated=outdated or None)
        drafted = AuthoringEngine(profile, agent).propose(inputs, source="standalone", proposal_id=pid)
    except (*REFUSALS, AgentError) as e:
        print(f"refused: {_why(e)}")
        return AUTHOR_REFUSED
    finally:
        if agent is not None and agent.last_call:
            store.event(pid, "T3", "agent.call", agent="cli-author", **agent.last_call)
    folder = out_root / pid
    h = save_proposal(folder, drafted)
    p = drafted.proposal
    store.event(pid, "T3", "author.proposed", proposal=h, changes=len(p.changes), dropped=len(p.dropped),
                spec_questions=len(p.spec_questions), dry_run=a.dry_run)
    _print_proposal(p, folder)
    if not p.changes:
        if p.proposed_count == 0:
            print("no changes proposed" + (": answer the spec questions, then run again" if p.spec_questions else ""))
            return AUTHOR_ONLY_QUESTIONS
        print("every proposed change was dropped by a guard; nothing to apply")
        return AUTHOR_ALL_DROPPED
    if a.dry_run:
        print(f"dry run: nothing written; apply after review with: ainxt-ath author apply {pid} "
              f"--profile {a.profile} --tests {a.tests}")
        return AUTHOR_OK
    return _author_apply(profile, p, tests, a.force, a.user, store)


def cmd_events(a: argparse.Namespace) -> int:
    for stage, kind, detail in StateStore(a.db).events(a.run_id):
        print(f"{stage:<4} {kind:<20} {detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ainxt-ath", description="AiNxt Test Harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a test profile")
    v.add_argument("profile")
    v.set_defaults(fn=cmd_validate)

    d = sub.add_parser("doctor", help="validate profile and check adapters")
    d.add_argument("profile")
    d.add_argument("--config", help="harness config (default: $ATH_CONFIG, .ath/config.yaml, ~/.config/ainxt/ath.yaml)")
    d.add_argument("--tests", help="test checkout: check testAuthoring globs and the testData catalogue against it")
    d.add_argument("--code", help="code checkout: check testAuthoring.codeContext globs against it")
    d.set_defaults(fn=cmd_doctor)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", required=True)
        p.add_argument("--db", default=".ath/state.db")
        p.add_argument("--package-version", default="dev")
        p.add_argument("--report-dir", default=".ath/reports")
        p.add_argument("--quarantine", help="file with one quarantined case id per line")
        p.add_argument("--config", help="harness config (default: $ATH_CONFIG, .ath/config.yaml, ~/.config/ainxt/ath.yaml)")

    r = sub.add_parser("run", help="start a run")
    common(r)
    r.add_argument("--commit", required=True)
    r.add_argument("--merge-base", required=True)
    r.add_argument("--env", required=True)
    r.add_argument("--mode", default="report-only", choices=[m.value for m in RunMode])
    r.add_argument("--mr")
    r.add_argument("--jira")
    r.add_argument("--changed", nargs="*", help="changed file paths (normally taken from the MR)")
    r.add_argument("--user", default="local-user", help="SSO user id (set by the platform)")
    r.add_argument("--run-id")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--diff", help="unified diff of the MR, for triage context (git diff <merge-base>..<commit>)")
    r.add_argument("--tsd", help="TSD markdown for triage context")
    r.add_argument("--rh-package", help="Requirement-Harness SDLC package (manifest.json or its folder): "
                                        "its approved TSD, hash-checked, is used as context")
    r.add_argument("--tests", help="local test checkout T3 writes approved test changes into")
    r.add_argument("--code", help="local code checkout; T3 sends its source signatures to the author model")
    sw = r.add_mutually_exclusive_group()
    sw.add_argument("--author", dest="author", action="store_const", const=True, default=None,
                    help="run T3 test authoring even if the profile has testAuthoring.enabled: false")
    sw.add_argument("--no-author", dest="author", action="store_const", const=False,
                    help="skip T3 test authoring in this run")
    r.set_defaults(fn=cmd_run)

    for name, decision, help_ in (("approve", "approved", "approve a gate and continue the run"),
                                  ("reject", "rejected", "reject a gate and stop the run")):
        g = sub.add_parser(name, help=help_)
        common(g)
        g.add_argument("run_id")
        g.add_argument("--gate", required=True, choices=["T3", "T10", "T11"], help="the gate the run waits at")
        g.add_argument("--user", required=True, help="who decides (SSO user id in the platform)")
        g.add_argument("--groups", help="comma-separated groups of that user (SSO groups in the platform)")
        g.add_argument("--comment", help="reason, recorded with the decision")
        if decision == "approved":
            g.add_argument("--ref", help="T3: the proposal hash you reviewed; refused if the stored proposal differs")
        g.set_defaults(fn=cmd_gate, decision=decision, ref=None)

    au = sub.add_parser("author", help="create or update tests from a TSD in a local checkout "
                                       "(exit 4: only spec questions, 5: every change dropped)")
    au.add_argument("action", nargs="?", choices=["apply"], help="apply: apply a stored proposal, no model call")
    au.add_argument("proposal_id", nargs="?", help="with apply: the proposal id (author-...)")
    au.add_argument("--profile", required=True)
    au.add_argument("--tests", required=True, help="the local test checkout")
    au.add_argument("--tsd", help="TSD markdown")
    au.add_argument("--rh-package", help="Requirement-Harness package: its approved TSD, hash-checked")
    au.add_argument("--from-run", help="a run id: update the tests its triage marked test-outdated, "
                                       "using its TSD and diff unless --tsd/--rh-package/--diff are given")
    au.add_argument("--section", action="append", help="only this TSD section (title or ⓘ id); repeatable")
    au.add_argument("--code", help="code checkout: send source signatures so new tests call real methods")
    au.add_argument("--diff", help="unified diff of the change under test")
    au.add_argument("--dry-run", action="store_true", help="store the proposal for review; write nothing")
    au.add_argument("--force", action="store_true",
                    help="write even if target files have uncommitted changes or the checkout is not a Git repo")
    au.add_argument("--out", help="folder for proposals (default .ath/authoring); each goes in <out>/<id>/")
    au.add_argument("--db", default=".ath/state.db")
    au.add_argument("--user", default=getpass.getuser(), help="recorded as who applied the changes")
    au.add_argument("--config", help="harness config (default: $ATH_CONFIG, .ath/config.yaml, ~/.config/ainxt/ath.yaml)")
    au.set_defaults(fn=cmd_author)

    rs = sub.add_parser("resume", help="resume a run from its last checkpoint")
    common(rs)
    rs.add_argument("run_id")
    rs.add_argument("--force", action="store_true",
                    help="take over a run still marked running because the process running it died")
    rs.set_defaults(fn=cmd_resume)

    rp = sub.add_parser("report", help="print a run's report")
    rp.add_argument("run_id")
    rp.add_argument("--db", default=".ath/state.db")
    rp.set_defaults(fn=cmd_report)

    e = sub.add_parser("eval", help="score triage voters on a labelled dataset (exit 3 if a gate fails)")
    e.add_argument("dataset", help="folder with cases/ and truth/, e.g. tests/evals/triage")
    e.add_argument("--profile", required=True, help="profile for masking, correlation keys and the model policy")
    e.add_argument("--config", help="harness config (default: $ATH_CONFIG, .ath/config.yaml, ~/.config/ainxt/ath.yaml)")
    e.add_argument("--out", help="folder for report.md and results.json (default .ath/evals/triage-<id>)")
    e.add_argument("--min-accuracy", type=float, help="fail if accuracy is below this, e.g. 0.8")
    e.add_argument("--max-missed-regressions", type=int, default=0,
                   help="fail if more real regressions get a non-blocking verdict (default 0)")
    e.add_argument("--max-error-rate", type=float, default=0.1,
                   help="fail if a larger share of voter calls errors or times out (default 0.1)")
    e.set_defaults(fn=cmd_eval)

    ev = sub.add_parser("events", help="print a run's structured events")
    ev.add_argument("run_id")
    ev.add_argument("--db", default=".ath/state.db")
    ev.set_defaults(fn=cmd_events)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if getattr(a, "db", None) and a.db != ":memory:":
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
