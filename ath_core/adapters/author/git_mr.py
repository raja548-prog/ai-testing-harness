"""Test authoring via Git merge request on the product's test repo (skeleton).

Uses the triggering user's Git credential through the server-side credential broker,
restricted to: create branch `ath/<run>/tests` + open MR on the test repo. Never merges.
"""
from __future__ import annotations

from pathlib import Path

from ath_core.adapters.base import TestAuthor, registry
from ath_core.schemas.authoring import AppliedResult, Proposal


@registry.register("author", "git-mr")
class GitMrAuthor(TestAuthor):
    def apply(self, proposal: Proposal, base_dir: Path | None, approved_by: str) -> AppliedResult:
        # TODO: broker.git(user=approved_by).clone / create_branch / commit / open_mr
        raise NotImplementedError("git-mr test authoring is not implemented yet")
