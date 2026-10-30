import pytest
import yaml

from ath_core.schemas import TestProfile
from ath_core.stages.select import path_matches, select_suites
from tests.conftest import LOCAL_PROFILE


@pytest.mark.parametrize("path, pattern, ok", [
    ("src/main/java/clearing/Dispute.java", "**/clearing/**", True),
    ("clearing/Foo.java", "**/clearing/**", True),       # top-level directory
    ("src/clearing", "**/clearing/**", True),            # the directory itself
    ("src/clearingx/Foo.java", "**/clearing/**", False),
    ("src/main/Foo.java", "src/*.java", True),           # `*` still crosses `/`, as with fnmatch
    ("docs/a.md", "docs/?.md", True),
    ("docs/a.md", "docs/[!a].md", False),
])
def test_path_globs(path, pattern, ok):
    assert path_matches(path, pattern) is ok


def test_no_changed_paths_runs_every_suite():
    profile = TestProfile.model_validate(yaml.safe_load(LOCAL_PROFILE.read_text()))
    assert select_suites(profile, []) == [s.id for s in profile.suites]
