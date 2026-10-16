import pytest
import yaml
from pydantic import ValidationError

from ath_core.schemas import TestProfile, load_profile
from tests.conftest import LOCAL_PROFILE, PROD_PROFILE


def test_example_profiles_validate():
    for path in (LOCAL_PROFILE, PROD_PROFILE):
        p = load_profile(path)
        assert p.product == "settlenxt"
        assert {s.id for s in p.suites} == {"smoke", "regression"}


def test_adapter_params_are_kept():
    p = load_profile(PROD_PROFILE)
    assert p.deploy.params["job"] == "settlenxt-cd-uat"
    assert p.logs.sources[0].params["keys"] == ["rrn", "stan", "batchId"]
    assert p.models.for_stage("triage") == "local/onprem-reasoner"
    assert p.models.for_stage("impact") == "local/onprem-coder"


def test_unknown_suite_in_selection_is_rejected(tmp_path):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["selection"]["default"] = ["nope"]
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="unknown suite 'nope'"):
        load_profile(f)


def test_unsupported_api_version_is_rejected():
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["apiVersion"] = "ainxt.io/test-profile/v9"
    with pytest.raises(ValidationError):
        TestProfile.model_validate(data)


def test_scripts_or_unknown_fields_are_rejected():
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["policy"]["preRunScript"] = "rm -rf /"
    with pytest.raises(ValidationError):
        TestProfile.model_validate(data)
