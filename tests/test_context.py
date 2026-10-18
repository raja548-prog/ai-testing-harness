"""Change context for triage: diff and TSD excerpts, Requirement-Harness packages, audit events."""
import hashlib
import json

import pytest

from ath_cli.main import main
from ath_core.context import diff_excerpt, read_rh_package, split_diff, tsd_excerpt
from ath_core.schemas import RunContext, load_profile
from ath_core.schemas.atrf import ATRFCase, ATRFFailure, CaseStatus, TriageCategory
from ath_server.orchestrator import Orchestrator, StateStore
from tests.conftest import LOCAL_PROFILE

DIFF = """diff --git a/portal/src/App.jsx b/portal/src/App.jsx
--- a/portal/src/App.jsx
+++ b/portal/src/App.jsx
@@ -1 +1 @@
-old label
+new label
diff --git a/clearing/FeeCalculator.java b/clearing/FeeCalculator.java
--- a/clearing/FeeCalculator.java
+++ b/clearing/FeeCalculator.java
@@ -41 +41 @@
-        return base.setScale(2, RoundingMode.HALF_UP);
+        return base.setScale(2, RoundingMode.DOWN);
"""
TSD = """# SNX-1840 TSD

## 2 Portal
Labels use full words.
ⓘ R-1

## 3.2 Fee rounding
Fees are rounded half-up to two decimals.
ⓘ R-3 · AC-7
"""
CASE = ATRFCase(id="SNX-001", name="Fee rounds half-up", suite="smoke", status=CaseStatus.FAILED,
                tags=["clearing"], failure=ATRFFailure(message="expected fee=1250.00 got 1249.99"))


def test_split_diff_by_file():
    assert [p for p, _ in split_diff(DIFF)] == ["portal/src/App.jsx", "clearing/FeeCalculator.java"]


def test_diff_excerpt_puts_the_related_file_first_within_budget():
    out = diff_excerpt(DIFF, CASE, max_chars=320)
    assert out.startswith("diff --git a/clearing/FeeCalculator.java") and "RoundingMode.DOWN" in out
    assert "App.jsx" not in out and "(1 more changed file(s) not shown)" in out


def test_tsd_excerpt_keeps_the_related_section_and_its_ids():
    out = tsd_excerpt(TSD, CASE, max_chars=90)
    assert out.startswith("## 3.2 Fee rounding") and "ⓘ R-3 · AC-7" in out and "Portal" not in out


def _rh_package(tmp_path, tsd=TSD, tamper=False):
    """The layout Requirement-Harness writes: <home>/packages/<ER>/manifest.json, <home>/er/<ER>/projections/."""
    home = tmp_path / "rh-home"
    proj = home / "er" / "ER-0007" / "projections"
    proj.mkdir(parents=True)
    (proj / "TSD.core.v1.md").write_text(tsd, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(tsd.encode()).hexdigest()
    if tamper:
        (proj / "TSD.core.v1.md").write_text(tsd + "\nAlso: fees may round down.\n", encoding="utf-8")
    pkg = home / "packages" / "ER-0007"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text(json.dumps({"er_id": "ER-0007", "product": "settlenxt", "projections": [
        {"checkpoint": "CP-TSD:core", "kind": "TSD", "repo_group": "core", "path": "projections/TSD.core.v1.md",
         "hash": digest},
        {"checkpoint": "CP-FSD", "kind": "FSD", "path": "projections/FSD.v1.md", "hash": "x"}]}))
    return pkg


def test_rh_package_gives_the_approved_tsd(tmp_path):
    text = read_rh_package(_rh_package(tmp_path))
    assert "Fees are rounded half-up" in text and "ER-0007 TSD (core)" in text


def test_rh_package_refuses_a_tsd_changed_after_sign_off(tmp_path):
    with pytest.raises(ValueError, match="changed after sign-off"):
        read_rh_package(_rh_package(tmp_path, tamper=True) / "manifest.json")


class Recorder:
    """A voter that records what it was shown."""
    seen: list = []

    def __init__(self, name):
        self.name = name
        self.last_call = {"model": "onprem-a", "prompt_sha256": "p" * 64}

    def classify(self, case, evidence):
        Recorder.seen.append(evidence)
        return TriageCategory.CODE_REGRESSION


def test_triage_receives_diff_and_tsd_excerpts_and_calls_are_audited():
    Recorder.seen.clear()
    store = StateStore(":memory:")
    o = Orchestrator(load_profile(LOCAL_PROFILE), store, agents=[Recorder(n) for n in "abc"])
    ctx = RunContext(run_id="x1", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                     triggered_by="dev")
    m = o.start(ctx, context={"diff": DIFF, "tsd": TSD + "\ncard 4111 1111 1111 1111\n"})
    ev = Recorder.seen[0]
    assert "diff --git" in ev["diff"] and "ⓘ" in ev["tsd"]
    assert "4111 1111 1111 1111" not in store.get_artifact("x1", "context/tsd")  # masked before storage
    assert set(m.inputs) == {"diff", "tsd"} and len(m.inputs["diff"]) == 64
    calls = [d for _, k, d in store.events("x1") if k == "agent.call"]
    assert len(calls) == len(Recorder.seen) >= 3  # one audit event per vote
    assert "prompt_sha256" in calls[0] and calls[0]["agent"] == "a"


def test_cli_run_with_diff_and_rh_package(tmp_path, capsys):
    diff = tmp_path / "mr.diff"
    diff.write_text(DIFF)
    rc = main(["run", "--profile", str(LOCAL_PROFILE), "--db", ":memory:", "--commit", "mr001",
               "--merge-base", "base000", "--env", "uat1", "--quiet", "--diff", str(diff),
               "--rh-package", str(_rh_package(tmp_path))])
    assert rc == 0
    bad = _rh_package(tmp_path / "t", tamper=True)
    assert main(["run", "--profile", str(LOCAL_PROFILE), "--db", ":memory:", "--commit", "mr001",
                 "--merge-base", "base000", "--env", "uat1", "--rh-package", str(bad)]) == 1
    assert "changed after sign-off" in capsys.readouterr().out
