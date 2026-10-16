"""T2 test selection: profile rules first. LLM additions (from T1) may only add suites."""
from __future__ import annotations

from fnmatch import fnmatch

from ath_core.schemas.profile import TestProfile


def select_suites(profile: TestProfile, changed_paths: list[str], llm_additions: list[str] | None = None) -> list[str]:
    chosen: list[str] = list(profile.selection.default)
    for rule in profile.selection.rules:
        if any(fnmatch(p, pat) for p in changed_paths for pat in rule.when.paths_changed):
            chosen += rule.run
    valid = {s.id for s in profile.suites}
    chosen += [s for s in (llm_additions or []) if s in valid]
    seen: set[str] = set()
    ordered = [s for s in chosen if not (s in seen or seen.add(s))]
    return ordered or [profile.suites[0].id]
