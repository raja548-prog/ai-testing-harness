"""T9 report: Markdown summary posted to the MR and Jira, next to the ATRF JSON."""
from __future__ import annotations

from ath_core.schemas.atrf import BLOCKING_CATEGORIES, ATRFRun
from ath_core.schemas.run import RunContext

# Markdown that untrusted text (test output, model output) could use to add links, HTML or emphasis.
_MD_SPECIAL = "\\`*_[]<>|"


def _text(value: str, limit: int | None = None) -> str:
    """Untrusted text as inline Markdown: one line (a newline would end a table row), every special
    character escaped. Truncated before escaping, so an escape is never cut in half."""
    value = " ".join(value.split())
    if limit is not None:
        value = value[:limit]
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in value)


def _code(value: str) -> str:
    """Untrusted text as a code span: a backtick would close it, a pipe would split the table cell."""
    return "`" + " ".join(value.split()).replace("`", "'").replace("|", "\\|") + "`"


def render_report(ctx: RunContext, runs: list[ATRFRun], notes: list[str] | None = None) -> str:
    lines = [f"## ATH report: {_text(ctx.product)}", ""]
    lines.append(f"Run {_code(ctx.run_id)} · mode `{ctx.mode.value}` · commit {_code(ctx.commit)} "
                 f"vs merge-base {_code(ctx.merge_base)} · env {_code(ctx.env)}")
    lines.append("")
    blocking, other = [], []
    lines += ["| Suite | Total | Passed | Failed | Error | Skipped |", "| --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        s = r.summary
        flag = " (incomplete)" if r.incomplete else ""
        lines.append(f"| {_text(r.suite)}{flag} | {s.total} | {s.passed} | {s.failed} | {s.error} | {s.skipped} |")
        for c in r.failing():
            (blocking if (c.triage is None or c.triage.category in BLOCKING_CATEGORIES) else other).append(c)
    lines.append("")
    incomplete = [r.suite for r in runs if r.incomplete]
    verdict = "BLOCKED" if blocking or incomplete else "GREEN"
    lines.append(f"**Verdict: {verdict}** ({len(blocking)} blocking, {len(other)} non-blocking failures)")
    if incomplete:
        lines += ["", f"Incomplete results: {_text(', '.join(incomplete))} stopped before every test ran "
                      f"(the job was aborted or broke), so tests that never ran could not be judged."]
    for title, group in (("Blocking failures", blocking), ("Non-blocking failures", other)):
        if not group:
            continue
        lines += ["", f"### {title}", "", "| Case | Suite | Category | Reason | Message |", "| --- | --- | --- | --- | --- |"]
        for c in group:
            cat = c.triage.category.value if c.triage else "untriaged"
            reason = _text(c.triage.reason) if c.triage else ""
            if c.triage and c.triage.evidence:
                reason += f'; evidence: "{_text(c.triage.evidence[0], 120)}"'
            msg = _text(c.failure.message if c.failure else "", 160)
            lines.append(f"| {_code(c.id)} | {_text(c.suite)} | {cat} | {reason} | {msg} |")
    if notes:
        lines += ["", "### Notes", ""] + [f"- {_text(n)}" for n in notes]
    return "\n".join(lines) + "\n"
