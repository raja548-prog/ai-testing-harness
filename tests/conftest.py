from pathlib import Path

import pytest

from ath_core.adapters.fake import FakeExecutor, FakeLock, FakePipeline

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROFILE = ROOT / "examples/settlenxt/test-profile.local.yaml"
PROD_PROFILE = ROOT / "examples/settlenxt/test-profile.yaml"
FIXTURES = ROOT / "tests/fixtures"


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    FakeExecutor.attempts.clear()
    FakeLock.held.clear()
    FakePipeline.triggered.clear()
    monkeypatch.chdir(ROOT)  # fixture paths in the local profile are repo-relative
    yield
