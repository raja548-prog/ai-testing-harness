"""Jenkins test report (GET <build>/testReport/api/json) to ATRF, with masking.

This is what the job's `junit` step published, so it works for any framework Jenkins can
read. Jenkins keeps class, name, status, duration, error text and (for failures) stdout;
it drops JUnit <properties>, tags and custom ids. Case ids are therefore `className.name`,
and correlation keys come from `key=value` lines in stdout.
"""
from __future__ import annotations

import json
from typing import Any

from ath_core.adapters.base import AdapterError, RawResults, ResultParser, registry
from ath_core.schemas.atrf import ATRFCase, ATRFFailure, ATRFRun, CaseStatus

from .junit_xml import correlation_from_text

_STATUS = {
    "PASSED": CaseStatus.PASSED, "FIXED": CaseStatus.PASSED,
    "FAILED": CaseStatus.FAILED, "REGRESSION": CaseStatus.FAILED,
    "SKIPPED": CaseStatus.SKIPPED,
}


@registry.register("parser", "jenkins-test-report")
class JenkinsTestReportParser(ResultParser):
    format = "jenkins-test-report"

    def parse(self, raw: RawResults, *, run_id, product, suite, env, commit, masker) -> ATRFRun:
        cases: list[ATRFCase] = []
        for fname, payload in sorted(raw.files.items()):
            try:
                body = json.loads(payload)
            except ValueError as e:
                raise AdapterError(f"{fname}: not a Jenkins test report ({e})", retryable=False)
            suites = list(body.get("suites") or [])
            for child in body.get("childReports") or []:  # matrix and multi-config jobs
                suites += ((child or {}).get("result") or {}).get("suites") or []
            for ts in suites:
                for tc in ts.get("cases") or []:
                    cases.append(self._case(tc, suite, masker))
        return ATRFRun(run_id=run_id, product=product, suite=suite, env=env, commit=commit, cases=cases)

    def _case(self, tc: dict[str, Any], suite: str, masker) -> ATRFCase:
        classname = masker.mask(tc.get("className") or "")  # ids are masked like names
        name = masker.mask(tc.get("name") or "unnamed")
        status = CaseStatus.SKIPPED if tc.get("skipped") else _STATUS.get(tc.get("status", ""), CaseStatus.FAILED)
        failure = None
        if status == CaseStatus.FAILED:
            trace = (tc.get("errorStackTrace") or "").strip()
            first = trace.splitlines()[0] if trace else ""
            exc = first.split(":", 1)[0].strip() if ":" in first else ""
            failure = ATRFFailure(
                type="assertion" if not exc or "Assert" in exc or "ComparisonFailure" in exc else masker.mask(exc),
                message=masker.mask(tc.get("errorDetails") or ""),
                trace=masker.mask(trace)[:4000],
            )
        return ATRFCase(
            id=f"{classname}.{name}" if classname else name, name=name, suite=suite,
            status=status, duration_ms=int(float(tc.get("duration") or 0) * 1000), failure=failure,
            correlation=correlation_from_text((tc.get("stdout") or "") + "\n" + (tc.get("stderr") or "")),
        )
