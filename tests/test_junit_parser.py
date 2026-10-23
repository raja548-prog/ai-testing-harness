from ath_core.adapters import RawResults
from ath_core.adapters.parsers.junit_xml import JUnitXmlParser
from ath_core.masking import Masker
from ath_core.schemas import CaseStatus
from tests.conftest import FIXTURES


def _parse(path):
    raw = RawResults("junit-xml", {path.name: path.read_bytes()})
    return JUnitXmlParser().parse(raw, run_id="r1", product="settlenxt", suite="smoke",
                                  env="uat1", commit="mr001", masker=Masker())


def test_statuses_and_ids():
    run = _parse(FIXTURES / "mr001/smoke/TEST-smoke.xml")
    s = run.summary
    assert (s.total, s.passed, s.failed) == (5, 1, 4)
    assert run.by_id()["SNX-005"].status == CaseStatus.PASSED


def test_failure_message_is_masked():
    case = _parse(FIXTURES / "mr001/smoke/TEST-smoke.xml").by_id()["SNX-001"]
    assert "4111" not in case.failure.message.replace("[PAN****1111]", "")
    assert "[PAN****1111]" in case.failure.message


def test_correlation_keys_from_properties_and_stdout():
    cases = _parse(FIXTURES / "mr001/smoke/TEST-smoke.xml").by_id()
    assert cases["SNX-001"].correlation == {"rrn": "629014773311"}
    assert cases["SNX-003"].correlation == {"batchId": "B20261016-07"}
    assert cases["SNX-001"].tags == ["clearing", "dispute"]


def test_testsuites_root():
    run = _parse(FIXTURES / "mr001/regression/TEST-regression.xml")
    assert run.summary.total == 2


def test_case_ids_are_masked():
    xml = b'<testsuite><testcase classname="Refund" name="card 4111111111111111"/></testsuite>'
    run = JUnitXmlParser().parse(RawResults("junit-xml", {"t.xml": xml}), run_id="r1", product="settlenxt",
                                 suite="smoke", env="uat1", commit="mr001", masker=Masker())
    assert run.cases[0].id == "Refund.card [PAN****1111]"
    # known failures and quarantine entries are matched after the same masking
    assert Masker().mask("Refund.card 4111111111111111") == run.cases[0].id


def _parse_files(files):
    raw = RawResults("junit-xml", {name: body.encode() for name, body in files.items()})
    return JUnitXmlParser().parse(raw, run_id="r1", product="settlenxt", suite="smoke",
                                  env="uat1", commit="mr001", masker=Masker())


def test_nested_suites_and_bare_testcases_are_found():
    run = _parse_files({"t.xml": """<testsuites>
      <testsuite name="outer"><testsuite name="inner">
        <testcase classname="A" name="deep"><failure message="boom"/></testcase>
      </testsuite></testsuite>
      <testcase classname="A" name="bare"/>
    </testsuites>"""})
    assert {c.id: c.status for c in run.cases} == {"A.deep": CaseStatus.FAILED, "A.bare": CaseStatus.PASSED}


def test_skip_marker_does_not_hide_a_failure():
    run = _parse_files({"t.xml": '<testsuite><testcase classname="A" name="t">'
                                 '<failure message="real"/><skipped/></testcase></testsuite>'})
    assert run.cases[0].status == CaseStatus.FAILED and run.cases[0].failure.message == "real"


def test_duplicate_ids_keep_the_worst_occurrence():
    passing = '<testsuite><testcase classname="A" name="t"/><testcase classname="A" name="u"/></testsuite>'
    failing = '<testsuite><testcase classname="A" name="t"><failure message="jdk11"/></testcase></testsuite>'
    for files in ({"1.xml": failing, "2.xml": passing}, {"1.xml": passing, "2.xml": failing}):
        run = _parse_files(files)
        assert [c.id for c in run.cases] == ["A.t", "A.u"]  # one case per id, first-seen order
        assert run.by_id()["A.t"].status == CaseStatus.FAILED
        assert run.summary.total == 2
