"""`ainxt-ath` CLI. Ships as the `ainxt ath ...` plugin of the AiNxt CLI.

Local development runs everything in-process against a SQLite state store. In the
platform, the same commands call the ATH API with the user's SSO token instead.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from ath_core.adapters import AdapterError, registry
from ath_core.adapters import loader  # noqa: F401
from ath_core.agents.cli import (
    AgentError, author_model, build_author_agent, build_triage_agents, diversity_problem, load_skill, model_family,
    model_problem, voter_models,
)
from ath_core.authoring.engine import (
    PLAN_SKILL, AuthorInputs, AuthoringEngine, DraftedPlan, Workspace, answer_sheet, answers_from,
    apply_plan_edits, load_answers, load_plan, load_proposal, plan_yaml, render_plan_md, save_plan, save_proposal,
)
from ath_core.authoring.formats import glob_match
from ath_core.authoring.testdata import load_catalogue
from ath_core.triage.voting import default_quorum
from ath_core.config import load_config
from ath_core.context import load_product_skill, read_rh_package
from ath_core.policy import load_policy
from ath_core.schemas import ATRFRun, RunContext, RunMode, Stage, TriageCategory, load_profile
from ath_core.schemas.authoring import Clarification, Proposal, TestPlan
from ath_server.orchestrator import Orchestrator, StateStore

KIND_FOR = {"build": "pipeline", "deploy": "pipeline"}
DEFAULT_DB = ".ath/state.db"
# Run artifacts a person reads to review T3 (all masked). Others, e.g. raw/ results, are never printed.
REVIEW_ARTIFACTS = ("report.md", "author/plan.md", "author/plan.yaml", "author/answers.yaml",
                    "author/changes.patch", "author/checklist.md")


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
                        author_factory=lambda skill: build_author_agent(cfg, profile, skill))


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


def _sheet(path: str, key: str) -> tuple[Any, str | None, str]:
    """(the `key` list, the plan hash the file names, the text) of an answers or plan file."""
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"{path} needs a `{key}:` list")
    return data[key], data.get("plan_sha256"), text


def cmd_answer(a: argparse.Namespace) -> int:
    """T3 in a run: answer the plan's spec questions, or send an edited plan. Not an approval."""
    try:
        orch = _orch(a)
        groups = [g.strip() for g in (a.groups or "").split(",") if g.strip()]
        body = orch.store.get_artifact(a.run_id, "author/plan.json")
        if not body:
            raise ValueError(f"run {a.run_id} has no T3 plan waiting for answers")
        plan = TestPlan.model_validate_json(body)
        answers, plan_text, ref = None, None, a.ref
        if a.answers and a.plan:
            raise ValueError("give --answers or --plan, not both")
        if a.plan:
            _, named, plan_text = _sheet(a.plan, "items")
            ref = ref or named
        elif a.answers:
            answers, named, _ = _sheet(a.answers, "answers")
            ref = ref or named
        elif not a.no_input and sys.stdin.isatty() and sys.stdout.isatty() and plan.spec_questions:
            _print_plan(plan, Path(f"<run {a.run_id} artifacts>/author"))
            print("\nType an answer, `a` to accept the assumption, or Enter to skip.")
            answers = []
            for q in plan.spec_questions:
                print(f"\n{q.id} {('[' + q.clause + '] ') if q.clause else ''}{q.question}")
                if q.assumption:
                    print(f"  assumption: {q.assumption}")
                text = _ask("  answer> ").strip()
                answers.append({"id": q.id, "answer": "accept" if text.lower() == "a" else text})
            ref = ref or plan.hash  # the plan just shown
        else:
            raise ValueError("give --answers FILE (template: artifact author/answers.yaml) or --plan FILE "
                             "(artifact author/plan.yaml), or run at a terminal to be asked")
        m = orch.answer(a.run_id, a.user, groups, ref, answers=answers, plan_yaml_text=plan_text)
    except (*REFUSALS, PermissionError) as e:
        print(f"refused: {_why(e)}")
        return 1
    return _print_result(m)


def cmd_report(a: argparse.Namespace) -> int:
    name = a.artifact or "report.md"
    if name not in REVIEW_ARTIFACTS:
        print(f"refused: --artifact is one of {', '.join(REVIEW_ARTIFACTS[1:])}")
        return 1
    body = StateStore(a.db).get_artifact(a.run_id, name)
    print(body or f"no {name} for {a.run_id}")
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
AUTHOR_OK, AUTHOR_REFUSED, AUTHOR_ONLY_QUESTIONS, AUTHOR_ALL_DROPPED, AUTHOR_PLAN_READY = 0, 1, 4, 5, 6
INPUT_KEYS = ("tsd", "rh_package", "from_run", "diff", "code", "section")

_ask = input  # the interactive prompt; tests replace it


def _interactive(a: argparse.Namespace) -> bool:
    return not a.no_input and sys.stdin.isatty() and sys.stdout.isatty()


def _edit_file(path: Path) -> None:
    """Open a file in the tester's editor ($VISUAL, $EDITOR, else vi) and wait for it to close."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)], check=False)


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
          f"{len(p.spec_questions)} spec question(s)" + (f", from plan {p.plan_id}" if p.plan_id else ""))
    print(f"  hash {p.hash}")
    for c in p.changes:
        items = f" {', '.join(c.plan_items)}" if c.plan_items else ""
        print(f"  {c.action:<6} {c.path}  [{', '.join(c.clauses)}]{items}")
        for flag in c.flags:
            print(f"         review: {flag}")
    for d in p.dropped:
        print(f"  DROPPED {d.path}: {d.reason}")
    for q in p.spec_questions:
        print(f"  QUESTION {('[' + q.clause + '] ') if q.clause else ''}{q.question}")
    if p.summary:
        print(f"  summary: {' '.join(p.summary.split())}")
    print(f"  review: {folder / 'changes.patch'} and {folder / 'checklist.md'}")


def _print_plan(p: TestPlan, folder: Path) -> None:
    print(f"plan {p.id}: {len(p.items)} item(s), {len(p.dropped)} dropped, {len(p.spec_questions)} open "
          f"spec question(s), {len(p.clarifications)} clarification(s), round {p.round}")
    print(f"  hash {p.hash}")
    for i in p.items:
        who = "  (added by tester)" if i.origin == "tester" else "  (edited by tester)" if i.edited else ""
        print(f"  {i.id:<5} {i.action:<6} {i.path}  [{', '.join(i.clauses)}]{who}")
        print(f"         {' '.join(i.scenario.split())}")
        if i.expected:
            print(f"         expect: {' '.join(i.expected.split())}")
        if i.clarifications:
            print(f"         relies on: {', '.join(i.clarifications)}")
    for d in p.dropped:
        print(f"  DROPPED {d.path}: {d.reason}")
    for q in p.spec_questions:
        print(f"  QUESTION {q.id} {('[' + q.clause + '] ') if q.clause else ''}{q.question}")
        if q.assumption:
            print(f"           assumption: {' '.join(q.assumption.split())}")
    if p.summary:
        print(f"  summary: {' '.join(p.summary.split())}")
    print(f"  review: {folder / 'plan.md'}; edit {folder / 'plan.yaml'}"
          + (f"; answer in {folder / 'answers.yaml'}" if p.spec_questions else ""))


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


class _Author:
    """One `ainxt-ath author` invocation: plan rounds, then generation, against one test checkout."""

    def __init__(self, a: argparse.Namespace, profile, tests: Path, out_root: Path, store: StateStore):
        self.a, self.profile, self.tests, self.out_root, self.store = a, profile, tests, out_root, store
        self._cfg = None

    @property
    def cfg(self):
        if self._cfg is None:
            self._cfg = load_config(self.a.config)
        return self._cfg

    def cmd(self, action: str, ref: str) -> str:
        """The command line for the next step, with this invocation's profile and checkout."""
        return (f"ainxt-ath author {action} {ref} --profile {self.a.profile} --tests {self.a.tests}"
                + (f" --out {self.a.out}" if self.a.out else "") + (f" --db {self.a.db}" if self.a.db != DEFAULT_DB else ""))

    # ------------------------------------------------------------------ inputs
    def spec_from_args(self) -> dict[str, Any]:
        """The inputs of this invocation, as absolute paths, to store with each plan."""
        def absolute(v: str | None) -> str | None:
            return str(Path(v).expanduser().resolve()) if v else None
        return {"tsd": absolute(self.a.tsd), "rh_package": absolute(self.a.rh_package), "from_run": self.a.from_run,
                "diff": absolute(self.a.diff), "code": absolute(self.a.code), "section": self.a.section or []}

    def spec_for(self, folder: Path) -> dict[str, Any]:
        """A plan's stored inputs; inputs given on this command line win."""
        path = folder / "inputs.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        given = self.spec_from_args()
        spec = {k: (given[k] or stored.get(k)) for k in INPUT_KEYS}
        spec["tsd_sha256"] = stored.get("tsd_sha256", "")
        return spec

    def read(self, spec: dict[str, Any]) -> AuthorInputs:
        tsd, diff, outdated = "", "", []
        if spec.get("from_run"):
            tsd, diff, outdated = _from_run(self.store, spec["from_run"], self.profile.product)
        if spec.get("tsd") or spec.get("rh_package"):
            parts = [Path(spec["tsd"]).read_text(encoding="utf-8")] if spec.get("tsd") else []
            if spec.get("rh_package"):
                parts.append(read_rh_package(spec["rh_package"]))
            tsd = "\n\n".join(parts)
        if spec.get("diff"):
            diff = Path(spec["diff"]).read_text(encoding="utf-8")
        if not tsd.strip():
            raise ValueError("no TSD: give --tsd, --rh-package, or --from-run with a run that has one")
        sha = hashlib.sha256(tsd.encode("utf-8")).hexdigest()
        if spec.get("tsd_sha256") and spec["tsd_sha256"] != sha:
            print("note: the TSD changed since the plan was made; planning with the TSD as it is now")
        spec["tsd_sha256"] = sha
        return AuthorInputs(tsd=tsd, tests_dir=self.tests, sections=spec.get("section") or [],
                            code_dir=Path(spec["code"]) if spec.get("code") else None, diff=diff or None,
                            outdated=outdated or None)

    def check_checkout(self) -> None:
        if not self.a.dry_run and not self.a.force and _git_dirty(self.tests, []) is None:
            raise ValueError(f"{self.tests} is not a Git repository, so uncommitted changes cannot be checked; "
                             f"pass --force to write anyway, or --dry-run to only propose")

    # ------------------------------------------------------------------ plan
    def save(self, drafted: DraftedPlan, spec: dict[str, Any]) -> Path:
        p = drafted.plan
        folder = self.out_root / p.id
        save_plan(folder, drafted, (self.cmd("generate", p.id), self.cmd("answer", p.id)))
        (folder / "inputs.json").write_text(json.dumps({k: spec.get(k) for k in (*INPUT_KEYS, "tsd_sha256")},
                                                       indent=2), encoding="utf-8")
        return folder

    def plan_round(self, spec: dict[str, Any], parent: TestPlan | None = None,
                   clarifications: list[Clarification] | None = None) -> tuple[TestPlan, Path]:
        """One model call to t03-plan. Raises REFUSALS or AgentError."""
        pid = f"plan-{uuid.uuid4().hex[:10]}"
        inputs = self.read(spec)
        inputs.clarifications = clarifications or None
        agent = build_author_agent(self.cfg, self.profile, PLAN_SKILL)
        try:
            drafted = AuthoringEngine(self.profile, planner=agent).plan(inputs, "standalone", pid, parent)
        finally:
            if agent.last_call:
                self.store.event(pid, "T3", "agent.call", agent="cli-planner", **agent.last_call)
        p = drafted.plan
        folder = self.save(drafted, spec)
        self.store.event(p.id, "T3", "author.planned", plan=p.hash, parent=p.parent, round=p.round,
                         items=len(p.items), dropped=len(p.dropped), spec_questions=len(p.spec_questions),
                         clarifications=len(p.clarifications))
        return p, folder

    def answer_round(self, plan: TestPlan, new: list[Clarification], spec: dict[str, Any]) -> tuple[TestPlan, Path]:
        limit = self.profile.test_authoring.limits.max_rounds
        if plan.round >= limit:
            raise ValueError(f"plan {plan.id} is round {plan.round}, the limit (testAuthoring.limits.maxRounds "
                             f"{limit}); generate from it, edit plan.yaml, or update the TSD")
        self.store.event(plan.id, "T3", "author.answered", plan=plan.hash, by=self.a.user,
                         answered=[c.id for c in new])
        print(f"planning again with {len(new)} answer(s)")
        return self.plan_round(spec, parent=plan, clarifications=[*plan.clarifications, *new])

    def prompt_answers(self, plan: TestPlan, folder: Path) -> list[Clarification]:
        print(f"\n{len(plan.spec_questions)} spec question(s). Type an answer, `a` to accept the assumption, "
              f"or Enter to skip.")
        typed: dict[str, str] = {}
        for q in plan.spec_questions:
            print(f"\n{q.id} {('[' + q.clause + '] ') if q.clause else ''}{q.question}")
            if q.assumption:
                print(f"  assumption: {q.assumption}")
            text = _ask("  answer> ").strip()
            typed[q.id] = "accept" if text.lower() == "a" else text
        ws = Workspace(self.profile, self.tests)
        new = answers_from([{"id": k, "answer": v} for k, v in typed.items()], plan, self.a.user, ws.masker.mask)
        # The record of what was typed, masked like everything else stored.
        (folder / "answers.yaml").write_text(answer_sheet(plan, self.cmd("answer", plan.id),
                                                          {c.id: c.answer for c in new}), encoding="utf-8")
        return new

    def edit_plan(self, plan: TestPlan, folder: Path, text: str) -> tuple[TestPlan, Path]:
        """A new plan version from tester edits (no model call); the same plan when nothing changed."""
        edited = apply_plan_edits(plan, text, Workspace(self.profile, self.tests), self.a.user,
                                  new_id=f"plan-{uuid.uuid4().hex[:10]}")
        if edited is None:
            return plan, folder
        spec = self.spec_for(folder)
        new_folder = self.save(DraftedPlan(edited, render_plan_md(edited)), spec)
        self.store.event(edited.id, "T3", "author.plan_edited", plan=edited.hash, parent=plan.id, by=self.a.user,
                         items=[i.id for i in edited.items if i.edited or i.origin == "tester"])
        print(f"plan edited: new version {edited.id}")
        return edited, new_folder

    def review(self, plan: TestPlan, folder: Path, spec: dict[str, Any]) -> int:
        """Show the plan; answer, edit and approve it at a terminal, or say what to do next."""
        while True:
            _print_plan(plan, folder)
            interactive = _interactive(self.a)
            if plan.spec_questions and interactive and plan.round < self.profile.test_authoring.limits.max_rounds:
                new = self.prompt_answers(plan, folder)
                if new:
                    plan, folder = self.answer_round(plan, new, spec)
                    continue
            if plan.spec_questions and not interactive:
                print(f"open spec questions: answer them in {folder / 'answers.yaml'}, then run:\n"
                      f"  {self.cmd('answer', plan.id)}\n"
                      f"or generate from the plan as it is: {self.cmd('generate', plan.id)}")
                return AUTHOR_ONLY_QUESTIONS
            if not plan.items:
                print("the plan has no items; nothing to generate" +
                      (" (answer the spec questions, or update the TSD)" if plan.spec_questions else ""))
                return AUTHOR_ONLY_QUESTIONS
            if self.a.yes:
                return self.generate(plan, folder, spec)
            if not interactive:
                print(f"plan ready for review: read {folder / 'plan.md'}, edit {folder / 'plan.yaml'} if needed, "
                      f"then run:\n  {self.cmd('generate', plan.id)}")
                return AUTHOR_PLAN_READY
            choice = _ask(f"\nGenerate tests for {len(plan.items)} plan item(s)? [Y]es / [n]o / [e]dit plan.yaml: ")
            choice = choice.strip().lower()[:1]
            if choice == "n":
                print(f"stopped; generate later with: {self.cmd('generate', plan.id)}")
                return AUTHOR_PLAN_READY
            if choice == "e":
                _edit_file(folder / "plan.yaml")
                try:
                    plan, folder = self.edit_plan(plan, folder, (folder / "plan.yaml").read_text(encoding="utf-8"))
                except ValueError as e:
                    print(f"plan.yaml not used: {e}")
                    (folder / "plan.yaml").write_text(plan_yaml(plan, self.cmd("generate", plan.id)), encoding="utf-8")
                continue
            return self.generate(plan, folder, spec)

    # ------------------------------------------------------------------ generate
    def generate(self, plan: TestPlan, folder: Path, spec: dict[str, Any]) -> int:
        if not plan.items:
            print(f"plan {plan.id} has no items; nothing to generate")
            return AUTHOR_ONLY_QUESTIONS
        self.check_checkout()
        self.store.event(plan.id, "T3", "author.plan_approved", plan=plan.hash, by=self.a.user)
        pid = f"author-{uuid.uuid4().hex[:10]}"
        agent = None
        try:
            inputs = self.read(spec)
            inputs.clarifications, inputs.plan = plan.clarifications or None, plan
            agent = build_author_agent(self.cfg, self.profile)
            drafted = AuthoringEngine(self.profile, agent).propose(inputs, source="standalone", proposal_id=pid)
        finally:
            if agent is not None and agent.last_call:
                self.store.event(pid, "T3", "agent.call", agent="cli-author", **agent.last_call)
        return self.finish(drafted, pid)

    def finish(self, drafted, pid: str) -> int:
        folder = self.out_root / pid
        h = save_proposal(folder, drafted)
        p = drafted.proposal
        self.store.event(pid, "T3", "author.proposed", proposal=h, plan=p.plan_sha256 or None, changes=len(p.changes),
                         dropped=len(p.dropped), spec_questions=len(p.spec_questions), dry_run=self.a.dry_run)
        _print_proposal(p, folder)
        if not p.changes:
            if p.proposed_count == 0:
                print("no changes proposed" + (": see the spec questions" if p.spec_questions else ""))
                return AUTHOR_ONLY_QUESTIONS
            print("every proposed change was dropped by a guard; nothing to apply")
            return AUTHOR_ALL_DROPPED
        if self.a.dry_run:
            print(f"dry run: nothing written; apply after review with: {self.cmd('apply', pid)}")
            return AUTHOR_OK
        return _author_apply(self.profile, p, self.tests, self.a.force, self.a.user, self.store)


def cmd_author(a: argparse.Namespace) -> int:
    """Plan tests from a TSD, answer its spec questions, then generate and apply them in a local checkout,
    without a run. `answer <plan>` plans again with answers, `generate <plan>` writes the tests of an
    (edited) plan, `apply <proposal>` applies a stored proposal without a model call."""
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
        if a.action and not a.proposal_id:
            what = "proposal" if a.action == "apply" else "plan"
            raise ValueError(f"give the {what} id: ainxt-ath author {a.action} <id>")
    except REFUSALS as e:
        print(f"refused: {_why(e)}")
        return AUTHOR_REFUSED
    run = _Author(a, profile, tests, out_root, store)
    try:
        if a.action == "apply":
            p = load_proposal(out_root / a.proposal_id)
            if p.product != profile.product:
                raise ValueError(f"proposal {p.id} is for {p.product!r}, not {profile.product!r}")
            print(f"applying proposal {p.id} (hash {p.hash}), no model call")
            return _author_apply(profile, p, tests, a.force, a.user, store)
        if a.action in ("answer", "generate"):
            folder = out_root / a.proposal_id
            plan = load_plan(folder)
            if plan.product != profile.product:
                raise ValueError(f"plan {plan.id} is for {plan.product!r}, not {profile.product!r}")
            spec = run.spec_for(folder)
            if a.action == "answer":
                sheet = Path(a.answers) if a.answers else folder / "answers.yaml"
                if not plan.spec_questions:
                    raise ValueError(f"plan {plan.id} has no open spec questions; generate from it")
                new = load_answers(sheet.read_text(encoding="utf-8"), plan, a.user,
                                   Workspace(profile, tests).masker.mask)
                if not new:
                    raise ValueError(f"no answers in {sheet}: write `answer:` for at least one question")
                plan, folder = run.answer_round(plan, new, spec)
                return run.review(plan, folder, spec)
            plan_file = Path(a.plan) if a.plan else folder / "plan.yaml"
            if plan_file.exists():
                plan, folder = run.edit_plan(plan, folder, plan_file.read_text(encoding="utf-8"))
            print(f"generating tests for plan {plan.id} ({len(plan.items)} item(s), hash {plan.hash})")
            return run.generate(plan, folder, spec)
        # a new plan
        run.check_checkout()
        spec = run.spec_from_args()
        plan, folder = run.plan_round(spec)
        return run.review(plan, folder, spec)
    except (*REFUSALS, AgentError) as e:
        print(f"refused: {_why(e)}")
        return AUTHOR_REFUSED


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

    au = sub.add_parser("author", help="plan, then create or update tests from a TSD in a local checkout "
                                       "(exit 4: open spec questions, 5: every change dropped, "
                                       "6: plan waiting for review)")
    au.add_argument("action", nargs="?", choices=["answer", "generate", "apply"],
                    help="answer: plan again with answers.yaml; generate: write the tests of a plan "
                         "(after plan.yaml edits); apply: apply a stored proposal, no model call")
    au.add_argument("proposal_id", nargs="?", metavar="id",
                    help="with answer/generate: the plan id (plan-...); with apply: the proposal id (author-...)")
    au.add_argument("--answers", help="with answer: the answers file (default <out>/<plan>/answers.yaml)")
    au.add_argument("--plan", help="with generate: the edited plan (default <out>/<plan>/plan.yaml)")
    au.add_argument("--yes", action="store_true",
                    help="approve a plan with no open questions and generate right away (one step, for scripts)")
    au.add_argument("--no-input", action="store_true", help="never prompt, even at a terminal")
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
    au.add_argument("--db", default=DEFAULT_DB)
    au.add_argument("--user", default=getpass.getuser(), help="recorded as who answered, approved and applied")
    au.add_argument("--config", help="harness config (default: $ATH_CONFIG, .ath/config.yaml, ~/.config/ainxt/ath.yaml)")
    au.set_defaults(fn=cmd_author)

    an = sub.add_parser("answer", help="T3 in a run: answer the plan's spec questions (T3 plans again) or "
                                       "send an edited plan; the run parks at the plan gate again")
    common(an)
    an.add_argument("run_id")
    an.add_argument("--user", required=True, help="who answers (SSO user id in the platform)")
    an.add_argument("--groups", help="comma-separated groups of that user (SSO groups in the platform)")
    an.add_argument("--answers", help="answers file (`ainxt-ath answer` writes the template, artifact "
                                      "author/answers.yaml); without it, prompts at a terminal")
    an.add_argument("--plan", help="an edited plan.yaml (artifact author/plan.yaml), instead of answers")
    an.add_argument("--ref", help="the plan hash you read; refused if the stored plan differs "
                                  "(default: the stored plan, when answering at the prompt)")
    an.add_argument("--no-input", action="store_true", help="never prompt")
    an.set_defaults(fn=cmd_answer)

    rs = sub.add_parser("resume", help="resume a run from its last checkpoint")
    common(rs)
    rs.add_argument("run_id")
    rs.add_argument("--force", action="store_true",
                    help="take over a run still marked running because the process running it died")
    rs.set_defaults(fn=cmd_resume)

    rp = sub.add_parser("report", help="print a run's report")
    rp.add_argument("run_id")
    rp.add_argument("--db", default=".ath/state.db")
    rp.add_argument("--artifact", help="print a T3 review artifact instead: " + ", ".join(REVIEW_ARTIFACTS[1:]))
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
