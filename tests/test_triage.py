import pytest

from ath_core.schemas import ATRFCase, ATRFFailure, ATRFRun, CaseStatus, TriageCategory
from ath_core.schemas.profile import KnownFailure
from ath_core.triage.precheck import apply_prechecks, mark_flaky
from ath_core.triage.voting import default_quorum, vote


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


REG, ENV, DATA, UNK = (TriageCategory.CODE_REGRESSION, TriageCategory.ENVIRONMENT, TriageCategory.TEST_DATA,
                       TriageCategory.UNKNOWN)


def _votes(*answers, quorum=None):
    return vote(_case(), [_Agent(str(i), a) for i, a in enumerate(answers)], {}, quorum=quorum)


def test_default_quorum_is_a_strict_majority_of_at_least_two():
    assert [default_quorum(n) for n in range(1, 6)] == [2, 2, 2, 3, 3]
    assert _votes(REG).category == UNK                      # one voter never decides alone by default
    assert _votes(REG, quorum=1).category == REG            # unless quorum: 1 says so
    assert _votes(ENV, ENV, ENV, REG).category == ENV       # 3 of 4
    assert _votes(ENV, ENV, REG, DATA, UNK).category == UNK  # 2 of 5 is not a majority
    assert _votes(ENV, ENV, REG, DATA, UNK, quorum=2).category == ENV


@pytest.mark.parametrize("answers", [(ENV, ENV, REG, REG), (ENV, REG), (ENV, ENV, UNK, UNK)])
def test_a_tie_for_first_is_never_a_verdict(answers):
    for order in (answers, answers[::-1]):  # voter order must not matter
        t = _votes(*order, quorum=1)
        assert t.category == UNK and "tied" in t.reason


def _failing(cid, message, type_="AssertionError"):
    return ATRFCase(id=cid, name=cid, suite="s", status=F, failure=ATRFFailure(type=type_, message=message))


def _one(case, commit):
    return ATRFRun(run_id="r", product="p", suite="s", env="uat1", commit=commit, cases=[case])


def test_pre_existing_needs_the_same_failure_on_the_baseline():
    base = _one(_failing("t", "fee mismatch 0.01 for batch B20261016-07"), "base")
    same = _one(_failing("t", "fee mismatch 0.02 for batch B20261023-01"), "mr")  # numbers and ids differ
    assert apply_prechecks(same, base, [], set()) == []
    assert same.cases[0].triage.category == TriageCategory.PRE_EXISTING
    other = _one(_failing("t", "NullPointerException in FeeCalculator", "java.lang.NullPointerException"), "mr")
    assert [c.id for c in apply_prechecks(other, base, [], set())] == ["t"]  # a new way to fail: triage it
