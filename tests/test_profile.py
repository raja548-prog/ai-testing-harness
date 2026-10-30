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


def test_masking_categories_are_checked(tmp_path):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["logs"]["masking"] = ["PAN", "cvv"]
    assert TestProfile.model_validate(data).logs.masking == ["pan", "cvv"]
    data["logs"]["masking"] = ["pan", "cvc"]
    with pytest.raises(ValidationError, match="unknown masking categories"):
        TestProfile.model_validate(data)


def _validate(change):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    change(data)
    return TestProfile.model_validate(data)


def test_known_failure_expiry_must_be_an_iso_date():
    def bad_date(d):
        d["policy"]["knownFailures"] = [{"caseId": "x", "ticket": "T-1", "expires": "31/12/2026"}]
    with pytest.raises(ValidationError, match="ISO date"):
        _validate(bad_date)


def test_log_source_keys_must_be_a_list():
    with pytest.raises(ValidationError, match="must be a list"):
        _validate(lambda d: d["logs"]["sources"][0].update(keys="rrn"))


def test_selection_default_cannot_be_empty():
    with pytest.raises(ValidationError, match="default"):
        _validate(lambda d: d["selection"].update(default=[]))
