"""Agent CLI process handling: timeouts, idle detection, process-tree kill, session resume, audit record."""
import os
import sys
import time

import pytest

from ath_core.agents.cli import AgentError, CliAgentRunner, load_skill
from ath_core.config import AgentRunnerConfig
from tests.conftest import ROOT

FAKE = str(ROOT / "tests/fakes/fake_agent_cli.py")
SKILL = load_skill(ROOT / "skills/t07-triage/SKILL.md")
posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX here")


def runner(mode: str, monkeypatch, **cfg) -> CliAgentRunner:
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    return CliAgentRunner(AgentRunnerConfig(
        command=[sys.executable, FAKE, "-p", "{prompt}"], passEnv=["FAKE_AGENT_MODE", "FAKE_AGENT_PIDS"], **cfg))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); check its state where /proc or ps can tell
        import subprocess
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")
    except OSError:
        return True


@posix_only
def test_timeout_kills_the_whole_process_tree(monkeypatch, tmp_path):
    pids = tmp_path / "pids"
    monkeypatch.setenv("FAKE_AGENT_PIDS", str(pids))
    r = runner("spawn", monkeypatch, timeoutSec=2)
    with pytest.raises(AgentError, match="timed out for 2s"):
        r.run(SKILL, {"case": "{}"})
    parent, child = map(int, pids.read_text().split())
    deadline = time.monotonic() + 5
    while (_alive(parent) or _alive(child)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(parent) and not _alive(child), "the CLI's child process outlived the session"
    assert r.last_call["stopped"] == "timeout"


def test_idle_timeout_stops_a_silent_session(monkeypatch):
    r = runner("silent", monkeypatch, timeoutSec=30, idleTimeoutSec=1)
    started = time.monotonic()
    with pytest.raises(AgentError, match="printed nothing for 1s"):
        r.run(SKILL, {"case": "{}"})
    assert time.monotonic() - started < 3


def test_idle_timeout_spares_a_session_that_keeps_printing(monkeypatch):
    r = runner("drip", monkeypatch, timeoutSec=30, idleTimeoutSec=1)
    assert r.run(SKILL, {"case": "{}"})["category"] == "environment"


def test_timed_out_session_is_resumed_with_the_same_id(monkeypatch):
    r = runner("hang", monkeypatch, timeoutSec=1, sessionArgs=["--session-id", "{session_id}"],
               resumeArgs=["-r", "{session_id}"], timeoutResumes=1)
    out = r.run(SKILL, {"case": "{}"})
    sid = r.last_call["session_id"]
    assert out["evidence"] == f"resumed={sid}" and len(sid) == 36
    assert r.last_call["attempts"] == 2 and r.last_call["stopped"] is None


def test_resumes_are_capped(monkeypatch):
    r = runner("hang", monkeypatch, timeoutSec=1, sessionArgs=["--session-id", "{session_id}"],
               resumeArgs=["--session-id", "{session_id}"], timeoutResumes=1)  # never sends -r: stays hung
    with pytest.raises(AgentError, match="after 1 resume"):
        r.run(SKILL, {"case": "{}"})


def test_resume_needs_a_session_placeholder():
    with pytest.raises(ValueError, match="resumeArgs with a {session_id}"):
        AgentRunnerConfig(command=["ainxt", "-p", "{prompt}"], timeoutResumes=1, resumeArgs=["-c"])


def test_last_call_is_an_audit_record_without_content(monkeypatch):
    r = runner("answer", monkeypatch)
    r.run(SKILL, {"case": '{"id": "SNX-001"}'}, model="onprem-a")
    call = r.last_call
    assert set(call) == {"model", "session_id", "prompt_sha256", "attempts", "elapsed_s", "exit_code",
                         "stopped", "response_sha256"}
    assert call["model"] == "onprem-a" and call["exit_code"] == 0 and len(call["prompt_sha256"]) == 64
    assert "SNX-001" not in str(call)  # hashes, never prompt or response text
