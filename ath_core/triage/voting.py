"""Multi-agent classification and the quorum rule (T7 agents, T8 verify)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from ath_core.schemas.atrf import ATRFCase, Triage, TriageCategory

AGENT_CATEGORIES = {
    TriageCategory.CODE_REGRESSION, TriageCategory.TEST_OUTDATED,
    TriageCategory.TEST_DATA, TriageCategory.ENVIRONMENT, TriageCategory.UNKNOWN,
}


@dataclass
class Vote:
    """One agent's answer. `evidence` is a quote ATH verified against the inputs it sent."""
    category: TriageCategory
    evidence: str = ""
    note: str = ""  # why the vote was discarded or is unknown
    error: bool = False  # the agent call failed (CLI error, timeout, no usable output): no opinion at all


class TriageAgent(Protocol):
    """One independent classifier. Implementations call the AiNxt model gateway or an agent CLI.

    Inputs are data only: masked ATRF case, masked log lines, diff, TSD excerpt.
    The agent must never execute or derive commands from that data.
    `classify` returns a Vote, or just a TriageCategory for agents without evidence.
    """
    name: str

    def classify(self, case: ATRFCase, evidence: dict[str, Any]) -> Vote | TriageCategory: ...


def _cast(agent: TriageAgent, case: ATRFCase, evidence: dict[str, Any]) -> Vote:
    name = getattr(agent, "name", "agent")
    try:
        out = agent.classify(case, evidence)
    except Exception as e:  # an agent error counts as an "unknown" vote, never a crash
        return Vote(TriageCategory.UNKNOWN, note=f"{name}: {e}"[:300], error=True)
    v = out if isinstance(out, Vote) else Vote(out)
    if v.category not in AGENT_CATEGORIES:
        return Vote(TriageCategory.UNKNOWN, note=f"{name}: category {getattr(v.category, 'value', v.category)!r} "
                                                 f"is not an agent category; vote discarded")
    return v


def default_quorum(voters: int) -> int:
    """At least 2 votes and a strict majority of the voters: 2 of 3, 3 of 4, 3 of 5."""
    return max(2, voters // 2 + 1)


def vote(case: ATRFCase, agents: list[TriageAgent], evidence: dict[str, Any], quorum: int | None = None) -> Triage:
    """The category `quorum` voters agree on (default: default_quorum); a tie for first never decides."""
    cast = [_cast(agent, case, evidence) for agent in agents]
    votes = [v.category.value for v in cast]
    notes = [v.note for v in cast if v.note]
    errors = sum(v.error for v in cast)
    quorum = quorum or default_quorum(len(votes))
    ranked = Counter(votes).most_common(2)
    top, n = ranked[0] if ranked else (TriageCategory.UNKNOWN.value, 0)
    if len(ranked) > 1 and ranked[1][1] == n:
        return Triage(category=TriageCategory.UNKNOWN, confidence=0.0, decided_by="vote",
                      reason=f"votes tied {n}-{n}; needs human review", votes=votes, notes=notes, errors=errors)
    if n >= quorum and top != TriageCategory.UNKNOWN.value:
        return Triage(category=TriageCategory(top), confidence=n / len(votes), decided_by="vote",
                      reason=f"{n} of {len(votes)} agents agreed", votes=votes,
                      evidence=[v.evidence for v in cast if v.category.value == top and v.evidence], notes=notes,
                      errors=errors)
    return Triage(category=TriageCategory.UNKNOWN, confidence=0.0, decided_by="vote",
                  reason=f"no quorum ({quorum} of {len(votes)} needed); needs human review", votes=votes, notes=notes,
                  errors=errors)
