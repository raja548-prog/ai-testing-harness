"""`ainxt-ath` CLI. Ships as the `ainxt ath ...` plugin of the AiNxt CLI.

Local development runs everything in-process against a SQLite state store. In the
platform, the same commands call the ATH API with the user's SSO token instead.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from ath_core.adapters import AdapterError, registry
from ath_core.adapters import loader  # noqa: F401
from ath_core.schemas import RunContext, RunMode, load_profile
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
            problems = registry.create(kind, name, **params).config_problems()
        bad += bool(problems)
        print(f"  [{'ok' if not problems else 'CONFIG'}] {where}: {kind} adapter '{name}'")
        for problem in problems:
            print(f"      - {problem}")
    # TODO (platform): check the user's Git/Jira profile tokens and env reachability.
    print("doctor: all adapters available and configured" if not bad else f"doctor: {bad} adapter(s) missing or misconfigured")
    return 0 if not bad else 2


def _orch(a: argparse.Namespace) -> Orchestrator:
    profile = load_profile(a.profile)
    store = StateStore(a.db)
    quarantined = set()
    if getattr(a, "quarantine", None):
        quarantined = {l.strip() for l in Path(a.quarantine).read_text().splitlines() if l.strip()}
    return Orchestrator(profile, store, package_version=a.package_version, quarantined=quarantined,
                        report_dir=a.report_dir)


def _print_result(m) -> int:
    print(f"run {m.context.run_id}: {m.status.value}")
    for st, res in m.stages.items():
        extra = f"  ({res.error})" if res.error else ""
        print(f"  {st.value:<4} {res.status.value:<12}{extra}")
    return 0 if m.status.value in ("succeeded", "waiting-gate") else 1


def cmd_run(a: argparse.Namespace) -> int:
    orch = _orch(a)
    ctx = RunContext(
        run_id=a.run_id or f"ath-{uuid.uuid4().hex[:10]}", product=orch.profile.product,
        mode=RunMode(a.mode), mr=a.mr, jira=a.jira, commit=a.commit, merge_base=a.merge_base,
        env=a.env, changed_paths=a.changed or [], triggered_by=a.user, trigger_source="cli",
    )
    try:
        m = orch.start(ctx)
    except (ValueError, AdapterError) as e:
        print(f"refused: {e}")
        return 1
    rc = _print_result(m)
    report = orch.store.get_artifact(ctx.run_id, "report.md")
    if report and not a.quiet:
        print("\n" + report)
    return rc


def cmd_resume(a: argparse.Namespace) -> int:
    return _print_result(_orch(a).resume(a.run_id))


def cmd_report(a: argparse.Namespace) -> int:
    body = StateStore(a.db).get_artifact(a.run_id, "report.md")
    print(body or f"no report for {a.run_id}")
    return 0 if body else 1


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
    d.set_defaults(fn=cmd_doctor)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", required=True)
        p.add_argument("--db", default=".ath/state.db")
        p.add_argument("--package-version", default="dev")
        p.add_argument("--report-dir", default=".ath/reports")
        p.add_argument("--quarantine", help="file with one quarantined case id per line")

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
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="resume a run from its last checkpoint")
    common(rs)
    rs.add_argument("run_id")
    rs.set_defaults(fn=cmd_resume)

    rp = sub.add_parser("report", help="print a run's report")
    rp.add_argument("run_id")
    rp.add_argument("--db", default=".ath/state.db")
    rp.set_defaults(fn=cmd_report)

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
