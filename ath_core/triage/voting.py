"""Multi-agent classification and the 2-of-3 rule (T7 agents, T8 verify)."""
from __future__ import annotations

from collections import Counter
from typing import Any, Protocol

from ath_core.schemas.atrf import ATRFCase, Triage, TriageCategory

AGENT_CATEGORIES = {
    TriageCategory.CODE_REGRESSION, TriageCategory.TEST_OUTDATED,
    TriageCategory.TEST_DATA, TriageCategory.ENVIRONMENT, TriageCategory.UNKNOWN,
}


class TriageAgent(Protocol):
    """One independent classifier. Implementations call the AiNxt model gateway.

    Inputs are data only: masked ATRF case, masked log lines, diff, TSD excerpt.
    The agent must never execute or derive commands from that data.
    """
    name: str

    def classify(self, case: ATRFCase, evidence: dict[str, Any]) -> TriageCategory: ...


def vote(case: ATRFCase, agents: list[TriageAgent], evidence: dict[str, Any], quorum: int = 2) -> Triage:
    votes: list[str] = []
    for agent in agents:
        try:
            cat = agent.classify(case, evidence)
            if cat not in AGENT_CATEGORIES:
                cat = TriageCategory.UNKNOWN
        except Exception:  # an agent error counts as an "unknown" vote, never a crash
            cat = TriageCategory.UNKNOWN
        votes.append(cat.value)
    top, n = Counter(votes).most_common(1)[0] if votes else (TriageCategory.UNKNOWN.value, 0)
    if n >= quorum and top != TriageCategory.UNKNOWN.value:
        return Triage(category=TriageCategory(top), confidence=n / len(votes), decided_by="vote",
                      reason=f"{n} of {len(votes)} agents agreed", votes=votes)
    return Triage(category=TriageCategory.UNKNOWN, confidence=0.0, decided_by="vote",
                  reason="no quorum; needs human review", votes=votes)
