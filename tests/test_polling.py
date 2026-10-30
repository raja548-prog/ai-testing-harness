"""Status polling of external jobs backs off up to `pollSec`, and respects the job's timeout."""
import pytest
import yaml

from ath_core.adapters import ExecStatus, ExecutionHandle, RawResults, registry
from ath_core.adapters.base import ExecutorAdapter
from ath_core.schemas import RunContext, TestProfile, load_profile
from ath_server.orchestrator import Orchestrator, StateStore
from tests.conftest import FIXTURES, LOCAL_PROFILE


class FakeTime:
    """A clock that only moves when the orchestrator sleeps."""
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.now += s

    def clock(self):
        return self.now


def _orch(profile=None):
    o = Orchestrator(profile or load_profile(LOCAL_PROFILE), StateStore(":memory:"))
    t = FakeTime()
    o._sleep, o._clock = t.sleep, t.clock
    return o, t


def _running(n):
    """A poll function that reports RUNNING n times, then DONE."""
    calls = iter([ExecStatus.RUNNING] * n + [ExecStatus.DONE])
    return lambda: next(calls)


def test_backoff_starts_fast_and_is_capped():
    o, t = _orch()
    assert o._wait(_running(8), timeout_min=60, max_interval=10) == ExecStatus.DONE
    assert t.sleeps == [2.0, 3.0, 4.5, 6.75, 10, 10, 10, 10]


def test_a_finished_job_is_not_slept_on():
    o, t = _orch()
    assert o._wait(_running(0), timeout_min=60) == ExecStatus.DONE and t.sleeps == []


def test_three_hour_suite_costs_hundreds_of_checks_not_thousands():
    o, t = _orch()
    polls = []

    def poll():
        polls.append(t.now)
        return ExecStatus.RUNNING if t.now < 3 * 3600 else ExecStatus.DONE

    assert o._wait(poll, timeout_min=200, max_interval=30) == ExecStatus.DONE
    assert len(polls) < 400  # was one check per second: ~10,800


def test_timeout_is_respected_and_the_last_sleep_is_clamped():
    o, t = _orch()
    assert o._wait(lambda: ExecStatus.RUNNING, timeout_min=0.5, max_interval=30) == ExecStatus.TIMEOUT
    assert sum(t.sleeps) == pytest.approx(30.0)  # never sleeps past the deadline


class SlowExecutor(ExecutorAdapter):
    """Reports RUNNING a few times before finishing, then serves the demo fixtures."""
    def start(self, suite, ctx, commit, attempt=0):
        return ExecutionHandle("slow-executor", f"{suite}@{commit}", ctx.run_id, suite, commit, {"left": 3})

    def poll(self, handle):
        handle.meta["left"] -= 1
        return ExecStatus.RUNNING if handle.meta["left"] > 0 else ExecStatus.DONE

    def collect(self, handle):
        folder = FIXTURES / handle.commit / handle.suite
        return RawResults("junit-xml", {p.name: p.read_bytes() for p in folder.glob("TEST-*.xml")})


@pytest.fixture
def slow_executor(monkeypatch):
    # Registered for this test only: the adapter registry is shared by the whole session.
    monkeypatch.setitem(registry._kinds["executor"], "slow-executor", SlowExecutor)


def test_suite_poll_interval_comes_from_its_executor(tmp_path, slow_executor):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["suites"] = [{"id": "smoke", "executor": {"adapter": "slow-executor", "pollSec": 2.5},
                       "results": {"format": "junit-xml"}}]
    data["selection"] = {"default": ["smoke"]}
    data["policy"]["flakyRetries"] = 0
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    o, t = _orch(load_profile(path))
    m = o.start(RunContext(run_id="p1", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                           triggered_by="dev"))
    assert m.status.value == "succeeded"
    assert t.sleeps and max(t.sleeps) <= 2.5 and t.sleeps[:2] == [2.0, 2.5]


@pytest.mark.parametrize("value", [0, 0.5, 601, "30", True, float("nan"), float("inf")])
@pytest.mark.parametrize("where", ["deploy", "executor"])
def test_invalid_poll_sec_is_rejected(where, value):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    (data["deploy"] if where == "deploy" else data["suites"][0]["executor"])["pollSec"] = value
    with pytest.raises(ValueError, match="pollSec"):
        TestProfile.model_validate(data)  # field validation: no load_profile needed


def test_poll_sec_is_a_field_not_an_adapter_param():
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["deploy"]["pollSec"] = 45
    p = TestProfile.model_validate(data)
    assert p.deploy.poll_sec == 45 and "pollSec" not in p.deploy.params


@pytest.mark.parametrize("where", ["build", "logs"])
def test_poll_sec_elsewhere_is_rejected(where):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    (data["build"] if where == "build" else data["logs"]["sources"][0])["pollSec"] = 60
    with pytest.raises(ValueError, match="pollSec only applies to deploy and suite executors"):
        TestProfile.model_validate(data)


def test_zero_max_duration_is_rejected():
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["suites"][0]["maxDurationMin"] = 0
    with pytest.raises(ValueError, match="maxDurationMin"):
        TestProfile.model_validate(data)
