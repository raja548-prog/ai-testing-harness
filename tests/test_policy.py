"""Model policy: which models may see which data (config/model-policy.yaml)."""
import pytest
import yaml

from ath_cli.main import main
from ath_core.agents.cli import build_triage_agents
from ath_core.config import AgentRunnerConfig, HarnessConfig
from ath_core.policy import load_policy
from ath_core.schemas import load_profile
from tests.conftest import LOCAL_PROFILE, PROD_PROFILE


def _policy(tmp_path, **extra) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({
        "dataClasses": {"restricted": {"allowed": ["local/*"]}, "public": {"allowed": ["*"]}},
        "stages": {"triage": "restricted", "impact": "public"}, **extra}))
    return str(path)


def test_default_policy_keeps_restricted_data_on_local_models():
    p = load_policy()
    assert p.allows("local/onprem-reasoner", "triage", "settlenxt")
    assert p.allows("onprem-llama-70b", "remediate", "settlenxt")
    assert not p.allows("claude-haiku-5-5", "triage", "settlenxt")
    assert not p.allows("", "triage", "settlenxt")


def test_unlisted_stage_is_restricted(tmp_path):
    p = load_policy(_policy(tmp_path))
    assert p.data_class("t99", "settlenxt") == "restricted"
    assert not p.allows("claude-x", "t99", "settlenxt")


def test_product_override_is_per_product(tmp_path):
    p = load_policy(_policy(tmp_path, productOverrides={"settlenxt": {"triage": "public"}}))
    assert p.allows("claude-x", "triage", "settlenxt")
    assert not p.allows("claude-x", "triage", "othernxt")


@pytest.mark.parametrize("bad, match", [
    ({"dataClasses": {"public": {"allowed": ["*"]}}}, "'restricted'"),
    ({"dataClasses": {"restricted": {"allowed": []}}, "stages": {"triage": "secret"}}, "undefined data classes"),
])
def test_invalid_policies_are_rejected(tmp_path, bad, match):
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match=match):
        load_policy(path)


def test_missing_policy_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_policy(tmp_path / "nope.yaml")


def test_triage_refused_for_cloud_voter():
    cfg = HarnessConfig(agentRunner=AgentRunnerConfig(command=["ainxt", "-p", "{prompt}"],
                                                      models=["claude-haiku-5-5"]))
    with pytest.raises(ValueError, match="stage 'triage' sees 'restricted' data"):
        build_triage_agents(cfg, load_profile(LOCAL_PROFILE))


def test_cli_run_refused_and_doctor_flags_policy(tmp_path, capsys):
    cfg = tmp_path / "ath.yaml"
    cfg.write_text(yaml.safe_dump({"modelPolicy": _policy(tmp_path), "agentRunner": {
        "command": ["ainxt", "-p", "{prompt}"], "models": ["claude-haiku-5-5"]}}))
    rc = main(["run", "--profile", str(LOCAL_PROFILE), "--config", str(cfg), "--db", ":memory:",
               "--commit", "mr001", "--merge-base", "base000", "--env", "uat1"])
    assert rc == 1 and "refused: model policy" in capsys.readouterr().out
    main(["doctor", str(LOCAL_PROFILE), "--config", str(cfg)])
    assert "not allowed: ['claude-haiku-5-5']" in capsys.readouterr().out


def test_doctor_checks_profile_stage_models(tmp_path, capsys):
    profile = yaml.safe_load(PROD_PROFILE.read_text())
    profile["models"]["remediate"] = "gpt-5"
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(profile))
    main(["doctor", str(path)])
    out = capsys.readouterr().out
    assert "[CONFIG] model policy" in out and "stage 'remediate'" in out and "'gpt-5'" in out
