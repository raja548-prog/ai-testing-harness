"""T9 report: Markdown summary posted to the MR and Jira, next to the ATRF JSON."""
from __future__ import annotations

from ath_core.schemas.atrf import BLOCKING_CATEGORIES, ATRFRun
from ath_core.schemas.run import RunContext


def render_report(ctx: RunContext, runs: list[ATRFRun], notes: list[str] | None = None) -> str:
    lines = [f"## ATH report: {ctx.product}", ""]
    lines.append(f"Run `{ctx.run_id}` · mode `{ctx.mode.value}` · commit `{ctx.commit}` "
                 f"vs merge-base `{ctx.merge_base}` · env `{ctx.env}`")
    lines.append("")
    blocking, other = [], []
    lines += ["| Suite | Total | Passed | Failed | Error | Skipped |", "| --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        s = r.summary
        flag = " (incomplete)" if r.incomplete else ""
        lines.append(f"| {r.suite}{flag} | {s.total} | {s.passed} | {s.failed} | {s.error} | {s.skipped} |")
        for c in r.failing():
            (blocking if (c.triage is None or c.triage.category in BLOCKING_CATEGORIES) else other).append(c)
    lines.append("")
    verdict = "BLOCKED" if blocking else "GREEN"
    lines.append(f"**Verdict: {verdict}** ({len(blocking)} blocking, {len(other)} non-blocking failures)")
    for title, group in (("Blocking failures", blocking), ("Non-blocking failures", other)):
        if not group:
            continue
        lines += ["", f"### {title}", "", "| Case | Suite | Category | Reason | Message |", "| --- | --- | --- | --- | --- |"]
        for c in group:
            cat = c.triage.category.value if c.triage else "untriaged"
            reason = c.triage.reason if c.triage else ""
            msg = (c.failure.message if c.failure else "").replace("|", "\\|")[:160]
            lines.append(f"| `{c.id}` | {c.suite} | {cat} | {reason} | {msg} |")
    if notes:
        lines += ["", "### Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"
