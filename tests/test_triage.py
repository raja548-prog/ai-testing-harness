from ath_core.schemas import ATRFCase, ATRFFailure, ATRFRun, CaseStatus, TriageCategory
from ath_core.schemas.profile import KnownFailure
from ath_core.triage.precheck import apply_prechecks, mark_flaky
from ath_core.triage.voting import vote


def _run(commit, statuses):
    return ATRFRun(run_id="r", product="p", suite="s", env="uat1", commit=commit, cases=[
        ATRFCase(id=i, name=i, suite="s", status=st,
                 failure=ATRFFailure(message="x") if st == CaseStatus.FAILED else None)
        for i, st in statuses.items()])


F, P = CaseStatus.FAILED, CaseStatus.PASSED


def test_precheck_order():
    mr = _run("mr", {"a": F, "b": F, "c": F, "d": F})
    base = _run("base", {"a": P, "b": F, "c": P, "d": F})
    known = [KnownFailure(caseId="d", ticket="T-1", expires="2099-01-01"),
             KnownFailure(caseId="a", ticket="T-0", expires="2000-01-01")]  # expired: ignored
    remaining = apply_prechecks(mr, base, known, quarantined={"c"})
    cats = {c.id: c.triage.category for c in mr.failing() if c.triage}
    assert cats == {"b": TriageCategory.PRE_EXISTING, "c": TriageCategory.QUARANTINED,
                    "d": TriageCategory.KNOWN_FAILURE}
    assert [c.id for c in remaining] == ["a"]
    assert mark_flaky(remaining, {"a"}) == [] and mr.by_id()["a"].triage.category == TriageCategory.FLAKY


class _Agent:
    def __init__(self, name, answer):
        self.name, self.answer = name, answer

    def classify(self, case, evidence):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _case():
    return ATRFCase(id="x", name="x", suite="s", status=F)


def test_vote_quorum():
    t = vote(_case(), [_Agent("a", TriageCategory.CODE_REGRESSION), _Agent("b", TriageCategory.CODE_REGRESSION),
                       _Agent("c", TriageCategory.TEST_DATA)], {})
    assert t.category == TriageCategory.CODE_REGRESSION and t.votes.count("code-regression") == 2


def test_vote_disagreement_and_agent_errors_become_unknown():
    t = vote(_case(), [_Agent("a", TriageCategory.CODE_REGRESSION), _Agent("b", RuntimeError("boom")),
                       _Agent("c", TriageCategory.TEST_DATA)], {})
    assert t.category == TriageCategory.UNKNOWN


def test_agent_cannot_return_precheck_only_categories():
    t = vote(_case(), [_Agent(n, TriageCategory.PRE_EXISTING) for n in "abc"], {})
    assert t.category == TriageCategory.UNKNOWN
