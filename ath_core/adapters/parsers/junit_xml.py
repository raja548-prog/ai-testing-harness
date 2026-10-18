"""JUnit XML (Surefire/Gradle/pytest style) to ATRF, with masking."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ath_core.adapters.base import AdapterError, RawResults, ResultParser, registry
from ath_core.schemas.atrf import ATRFCase, ATRFFailure, ATRFRun, CaseStatus

# Correlation keys extracted from <properties> or system-out lines like "rrn=629014773311".
CORRELATION_KEYS = ("rrn", "stan", "batchId", "fileName", "traceId")


def correlation_from_text(text: str) -> dict[str, str]:
    """Correlation keys from lines of test output that start with `key=value`."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        for key in CORRELATION_KEYS:
            prefix = f"{key}="
            if line.startswith(prefix) and line[len(prefix):].split():
                found.setdefault(key, line[len(prefix):].split()[0])
    return found


@registry.register("parser", "junit-xml")
class JUnitXmlParser(ResultParser):
    format = "junit-xml"

    def parse(self, raw: RawResults, *, run_id, product, suite, env, commit, masker) -> ATRFRun:
        cases: list[ATRFCase] = []
        for fname, payload in sorted(raw.files.items()):
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as e:
                raise AdapterError(f"{fname}: not valid JUnit XML ({e})", retryable=False)
            suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
            for ts in suites:
                for tc in ts.findall("testcase"):
                    cases.append(self._case(tc, suite, masker))
        return ATRFRun(run_id=run_id, product=product, suite=suite, env=env, commit=commit, cases=cases)

    def _case(self, tc: ET.Element, suite: str, masker) -> ATRFCase:
        classname = tc.get("classname", "")
        name = tc.get("name", "unnamed")
        case_id = tc.get("id") or (f"{classname}.{name}" if classname else name)
        status, failure = CaseStatus.PASSED, None
        for tag, st in (("failure", CaseStatus.FAILED), ("error", CaseStatus.ERROR)):
            el = tc.find(tag)
            if el is not None:
                status = st
                failure = ATRFFailure(
                    type=el.get("type", "assertion" if tag == "failure" else "error"),
                    message=masker.mask(el.get("message", "")),
                    trace=masker.mask((el.text or "").strip())[:4000],
                )
        if tc.find("skipped") is not None:
            status = CaseStatus.SKIPPED
        correlation: dict[str, str] = {}
        props = tc.find("properties")
        if props is not None:
            for p in props.findall("property"):
                if p.get("name") in CORRELATION_KEYS and p.get("value"):
                    correlation[p.get("name")] = p.get("value")
        for key, value in correlation_from_text(tc.findtext("system-out") or "").items():
            correlation.setdefault(key, value)
        tags = [t.strip() for t in (tc.get("tags") or "").split(",") if t.strip()]
        return ATRFCase(
            id=case_id, name=masker.mask(name), suite=suite, status=status,
            duration_ms=int(float(tc.get("time", "0") or 0) * 1000), tags=tags,
            failure=failure, correlation=correlation,
        )
