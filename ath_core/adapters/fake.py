"""Fake adapters for local runs, demos and CI of the harness itself.

fake-executor reads JUnit files from: {fixtures}/{commit}/{suite}/*.xml
A file named `retry-<n>.xml` in that folder is returned on the n-th rerun (flaky simulation).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ath_core.adapters.base import (
    EnvLock, ExecStatus, ExecutionHandle, ExecutorAdapter, LogSource, MaskedLogs,
    PipelineAdapter, RawResults, registry,
)
from ath_core.schemas.run import RunContext


@registry.register("pipeline", "fake-pipeline")
class FakePipeline(PipelineAdapter):
    triggered: list[tuple[str, str]] = []

    def trigger(self, ctx: RunContext, commit: str, extra: dict[str, Any] | None = None) -> ExecutionHandle:
        FakePipeline.triggered.append((ctx.run_id, commit))
        return ExecutionHandle("fake-pipeline", f"job-{ctx.run_id}-{commit}", ctx.run_id, "deploy", commit)

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        return ExecStatus.FAILED if self.params.get("fail") else ExecStatus.DONE


@registry.register("executor", "fake-executor")
class FakeExecutor(ExecutorAdapter):
    attempts: dict[tuple[str, str], int] = {}

    def start(self, suite: str, ctx: RunContext, commit: str) -> ExecutionHandle:
        key = (commit, suite)
        n = FakeExecutor.attempts.get(key, 0)
        FakeExecutor.attempts[key] = n + 1
        return ExecutionHandle("fake-executor", f"{suite}@{commit}#{n}", ctx.run_id, suite, commit, {"attempt": n})

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        return ExecStatus.DONE

    def collect(self, handle: ExecutionHandle) -> RawResults:
        folder = Path(self.params["fixtures"]) / handle.commit / handle.suite
        attempt = handle.meta.get("attempt", 0)
        retry_file = folder / f"retry-{attempt}.xml"
        if attempt > 0 and retry_file.exists():
            files = {retry_file.name: retry_file.read_bytes()}
        else:
            files = {p.name: p.read_bytes() for p in sorted(folder.glob("*.xml")) if not p.name.startswith("retry-")}
        return RawResults(format="junit-xml", files=files)


@registry.register("logs", "fake-logs")
class FakeLogs(LogSource):
    def fetch(self, start: datetime, end: datetime, keys: dict[str, str], limit_lines: int, masker) -> MaskedLogs:
        raw = [f"{start.isoformat()} ERROR settlement failed for {k}={v} card=4111111111111111" for k, v in keys.items()]
        return MaskedLogs(source="fake-logs", lines=[masker.mask(x) for x in raw][:limit_lines])


@registry.register("envlock", "fake-lock")
class FakeLock(EnvLock):
    held: dict[str, str] = {}

    def acquire(self, env: str, run_id: str, wait_max_min: int) -> bool:
        owner = FakeLock.held.get(env)
        if owner and owner != run_id:
            return False
        FakeLock.held[env] = run_id
        return True

    def renew(self, env: str, run_id: str) -> bool:
        return FakeLock.held.get(env) == run_id

    def release(self, env: str, run_id: str) -> None:
        if FakeLock.held.get(env) == run_id:
            del FakeLock.held[env]
