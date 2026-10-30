"""T2 test selection: profile rules first. LLM additions (from T1) may only add suites."""
from __future__ import annotations

import re
from functools import lru_cache

from ath_core.schemas.profile import TestProfile


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    """Path glob: `**/` and `/**` also match zero directories, so `**/clearing/**` covers
    `clearing/Foo.java` and `src/clearing`. `*` and `?` keep fnmatch's meaning (`*` may cross `/`):
    a rule never matches fewer paths than it did with fnmatch."""
    out = ""
    for part in re.split(r"(\*\*/|/\*\*$|\*+|\?|\[!?[^\]]*\])", pattern):
        if part == "**/":
            out += "(?:.*/)?"
        elif part == "/**":
            out += "(?:/.*)?"
        elif part and set(part) == {"*"}:
            out += ".*"
        elif part == "?":
            out += "."
        elif part.startswith("[") and part.endswith("]") and len(part) > 2:
            out += "[^" + part[2:-1] + "]" if part.startswith("[!") else part
        else:
            out += re.escape(part)
    return re.compile(out)


def path_matches(path: str, pattern: str) -> bool:
    return _glob(pattern).fullmatch(path) is not None


def select_suites(profile: TestProfile, changed_paths: list[str], llm_additions: list[str] | None = None) -> list[str]:
    """Suites for the change. With no changed paths at all, nothing says the change is small: every suite runs."""
    if not changed_paths:
        return [s.id for s in profile.suites]
    chosen: list[str] = list(profile.selection.default)
    for rule in profile.selection.rules:
        if any(path_matches(p, pat) for p in changed_paths for pat in rule.when.paths_changed):
            chosen += rule.run
    valid = {s.id for s in profile.suites}
    chosen += [s for s in (llm_additions or []) if s in valid]
    seen: set[str] = set()
    return [s for s in chosen if not (s in seen or seen.add(s))]
