"""Adapter contracts. The core talks only to these interfaces.

Rules every adapter follows:
- `start`/`trigger` are idempotent on the run id (a resumed stage never starts a second job).
- Hard timeouts come from the profile.
- Credentials are fetched by reference (vault / credential broker), never from the profile.
- Anything returned to the core that can contain test data is masked first.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, TypeVar

from ath_core.schemas.atrf import ATRFRun
from ath_core.schemas.run import RunContext


class AdapterError(RuntimeError):
    """Raised for adapter failures the orchestrator should treat as retryable or fatal."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ExecStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ExecutionHandle:
    """Opaque reference to an external job; persisted in checkpoints so a restart can poll it."""
    adapter: str
    job_ref: str
    run_id: str
    suite: str
    commit: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawResults:
    """Raw result payloads exactly as the executor produced them (unmasked, transient)."""
    format: str
    files: dict[str, bytes]


@dataclass
class MaskedLogs:
    source: str
    lines: list[str]
    truncated: bool = False


class PipelineAdapter(ABC):
    """Triggers CI and CD jobs (e.g. Jenkins, GitLab CI)."""

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def trigger(self, ctx: RunContext, commit: str, extra: dict[str, Any] | None = None) -> ExecutionHandle: ...

    @abstractmethod
    def poll(self, handle: ExecutionHandle) -> ExecStatus: ...


class ExecutorAdapter(ABC):
    """Runs one suite against a deployed environment."""

    def __init__(self, **params: Any):
        self.params = params

    def preflight(self, ctx: RunContext) -> list[str]:
        """Return a list of problems; empty means ready."""
        return []

    @abstractmethod
    def start(self, suite: str, ctx: RunContext, commit: str) -> ExecutionHandle: ...

    @abstractmethod
    def poll(self, handle: ExecutionHandle) -> ExecStatus: ...

    @abstractmethod
    def collect(self, handle: ExecutionHandle) -> RawResults: ...

    def cancel(self, handle: ExecutionHandle) -> None:  # optional
        return None


class ResultParser(ABC):
    """Converts raw results into ATRF. Must mask via the masker it is given."""
    format: str = ""

    @abstractmethod
    def parse(self, raw: RawResults, *, run_id: str, product: str, suite: str, env: str,
              commit: str, masker: Any) -> ATRFRun: ...


class LogSource(ABC):
    """Fetches masked log lines for a time window and correlation keys."""

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def fetch(self, start: datetime, end: datetime, keys: dict[str, str], limit_lines: int,
              masker: Any) -> MaskedLogs: ...


class EnvLock(ABC):
    """Department-owned environment hold."""

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def acquire(self, env: str, run_id: str, wait_max_min: int) -> bool: ...

    @abstractmethod
    def renew(self, env: str, run_id: str) -> bool: ...

    @abstractmethod
    def release(self, env: str, run_id: str) -> None: ...


class TestAuthor(ABC):
    """Persists new or changed test cases (Git MR, test-server API, simulator scenarios)."""
    __test__ = False

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def propose(self, cases: list[dict[str, Any]], ctx: RunContext) -> dict[str, Any]: ...

    @abstractmethod
    def apply(self, proposal: dict[str, Any], approved_by: str) -> str: ...


T = TypeVar("T")


class _Registry:
    """Maps adapter names used in profiles to implementation classes."""

    def __init__(self) -> None:
        self._kinds: dict[str, dict[str, type]] = {}

    def register(self, kind: str, name: str) -> Callable[[type[T]], type[T]]:
        def deco(cls: type[T]) -> type[T]:
            self._kinds.setdefault(kind, {})[name] = cls
            return cls
        return deco

    def get(self, kind: str, name: str) -> type:
        try:
            return self._kinds[kind][name]
        except KeyError:
            known = sorted(self._kinds.get(kind, {}))
            raise AdapterError(f"no {kind} adapter named {name!r}; known: {known}", retryable=False)

    def create(self, kind: str, name: str, **params: Any) -> Any:
        return self.get(kind, name)(**params)

    def names(self, kind: str) -> list[str]:
        return sorted(self._kinds.get(kind, {}))


registry = _Registry()
