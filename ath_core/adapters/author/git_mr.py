"""Test authoring via Git merge request on the product's test repo (skeleton).

Uses the triggering user's Git credential through the server-side credential broker,
restricted to: create branch + open MR on the test repo. Never merges.
"""
from __future__ import annotations

from typing import Any

from ath_core.adapters.base import TestAuthor, registry
from ath_core.schemas.run import RunContext


@registry.register("author", "git-mr")
class GitMrAuthor(TestAuthor):
    def propose(self, cases: list[dict[str, Any]], ctx: RunContext) -> dict[str, Any]:
        # Draft only: returns the diff + review checklist; nothing is pushed before the T3 gate.
        return {"kind": "git-mr", "branch": f"ath/{ctx.run_id}/tests", "cases": cases}

    def apply(self, proposal: dict[str, Any], approved_by: str) -> str:
        # TODO: broker.git(user=ctx.triggered_by).create_branch / commit / open_mr
        raise NotImplementedError
