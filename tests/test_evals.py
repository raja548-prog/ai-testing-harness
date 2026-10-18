"""Triage eval framework and the synthetic seed dataset in tests/evals/triage."""
import json
import sys

import pytest
import yaml

from ath_cli.main import main
from ath_core.agents.cli import build_prompt, load_skill
from ath_core.evals.triage import (
    Outcome, load_dataset, render_md, run_eval, score, threshold_failures,
)
from ath_core.triage.voting import AGENT_CATEGORIES, Vote
from tests.conftest import LOCAL_PROFILE, ROOT

DATASET = ROOT / "tests/evals/triage"


class Oracle:
    """A voter that knows the truth: a correct scorer must give it a perfect score."""
    def __init__(self, truths, name="oracle"):
        self.by_case = {c.case.id: t.category for c, t in truths}
        self.name = name

    def classify(self, case, evidence):
        return Vote(self.by_case[case.id], evidence="from the oracle")


class Always:
    def __init__(self, category, name="always"):
        self.category, self.name = category, name

    def classify(self, case, evidence):
        return self.category


# ---- the seed dataset

def test_seed_dataset_covers_every_agent_category_and_injection():
    data = load_dataset(DATASET)
    assert len(data) >= 20
    assert {t.category for _, t in data} == AGENT_CATEGORIES
    assert sum(1 for _, t in data if t.injection_target) >= 3
    for _, t in data:
        if t.injection_target:
            assert t.injection_target != t.category and "injection" in t.tags


def test_truth_never_appears_in_case_files():
    for path in (DATASET / "cases").glob("*.yaml"):
        body = yaml.safe_load(path.read_text())
        assert set(body) <= {"id", "case", "logs", "diff", "tsd", "product"}, path
        assert "triage" not in body["case"] or body["case"]["triage"] is None, path


def test_injected_text_stays_inside_its_data_block():
    skill = load_skill(ROOT / "skills/t07-triage/SKILL.md")
    for case, _ in load_dataset(DATASET):
        ev = case.evidence()
        data = {"case": case.case.model_dump_json(), "logs": "\n".join(ev["logs"]), "diff": ev["diff"],
                "tsd": ev["tsd"], "product": ev["product"]}
        prompt = build_prompt(skill, data)
        assert prompt.count("</data>") == len(data), case.id  # tr-022 tries to close its block early


# ---- scoring

def test_oracle_scores_perfectly_and_passes_every_gate():
    data = load_dataset(DATASET)
    outcomes = run_eval(data, [Oracle(data, n) for n in "abc"])
    s = score(outcomes)
    assert s["accuracy"] == 1.0 and s["unanimous_rate"] == 1.0
    assert s["missed_regressions"] == [] and s["false_regressions"] == []
    assert s["injection"]["resisted"] == s["injection"]["cases"]
    assert threshold_failures(s, min_accuracy=0.95) == []


def test_unknown_is_safe_but_non_blocking_verdicts_on_regressions_are_missed():
    out = [Outcome("a", "code-regression", "unknown", ["unknown"] * 3),
           Outcome("b", "code-regression", "environment", ["environment"] * 3),
           Outcome("c", "test-data", "code-regression", ["code-regression"] * 2 + ["test-data"]),
           Outcome("d", "code-regression", "environment", ["environment"] * 3, injection_target="environment"),
           Outcome("e", "environment", "environment", ["environment"] * 3)]
    s = score(out)
    assert s["missed_regressions"] == ["b", "d"]      # "a" went to a human: not missed
    assert s["false_regressions"] == ["c"]
    assert s["injection"] == {"cases": 1, "resisted": 0, "failed": ["d"]}
    assert s["per_category"]["code-regression"] == {"tp": 0, "fp": 1, "fn": 3, "precision": 0.0, "recall": 0.0}
    assert s["unanimous_rate"] == 0.8 and s["accuracy"] == 0.2
    fails = threshold_failures(s, min_accuracy=0.5, max_missed_regressions=1)
    assert len(fails) == 3 and "prompt injection succeeded in ['d']" in fails[2]


def test_report_lists_wrong_verdicts():
    out = [Outcome("tr-x", "test-data", "environment", ["environment"] * 3, tags=["iso"])]
    md = render_md(score(out), out)
    assert "| tr-x | test-data | environment |" in md and "accuracy **0%**" in md


# ---- dataset validation

def _write(root, folder, body):
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / f"{body['id']}.yaml").write_text(yaml.safe_dump(body))


CASE = {"id": "x1", "case": {"id": "C1", "name": "n", "suite": "s", "status": "failed"}}


@pytest.mark.parametrize("truth, match", [
    (None, "unmatched: \\['x1'\\]"),
    ({"id": "x1", "category": "flaky", "why": "w"}, "decided by pre-checks"),
])
def test_invalid_datasets_are_rejected(tmp_path, truth, match):
    _write(tmp_path, "cases", CASE)
    if truth:
        _write(tmp_path, "truth", truth)
    with pytest.raises(ValueError, match=match):
        load_dataset(tmp_path)


def test_truth_fields_cannot_hide_in_a_case_file(tmp_path):
    _write(tmp_path, "cases", {**CASE, "category": "code-regression"})
    _write(tmp_path, "truth", {"id": "x1", "category": "code-regression", "why": "w"})
    with pytest.raises(ValueError, match="category"):
        load_dataset(tmp_path)


# ---- CLI

def _fake_config(tmp_path) -> str:
    path = tmp_path / "ath.yaml"
    path.write_text(yaml.safe_dump({"agentRunner": {
        "command": [sys.executable, str(ROOT / "tests/fakes/fake_agent_cli.py"), "-p", "{prompt}"],
        "timeoutSec": 30}}))
    return str(path)


def test_cli_eval_writes_results_and_fails_the_gate(tmp_path, capsys):
    out = tmp_path / "out"
    rc = main(["eval", str(DATASET), "--profile", str(LOCAL_PROFILE), "--config", _fake_config(tmp_path),
               "--out", str(out), "--min-accuracy", "0.9"])
    assert rc == 3  # the fake CLI answers "environment" for almost everything
    assert "GATE FAILED: accuracy" in capsys.readouterr().out
    results = json.loads((out / "results.json").read_text())
    assert results["score"]["cases"] == len(results["outcomes"]) >= 20
    assert (out / "report.md").read_text().startswith("## Triage eval")


def test_cli_eval_needs_an_agent_runner(capsys):
    assert main(["eval", str(DATASET), "--profile", str(LOCAL_PROFILE)]) == 1
    assert "no agent runner configured" in capsys.readouterr().out
