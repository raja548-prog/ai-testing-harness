"""Jenkins pipeline/executor adapter (skeleton: wire to your Jenkins REST API)."""
from __future__ import annotations

from typing import Any

import httpx

from ath_core.adapters.base import (
    AdapterError, ExecStatus, ExecutionHandle, ExecutorAdapter, PipelineAdapter, RawResults, registry,
)
from ath_core.schemas.run import RunContext


def _client(params: dict[str, Any]) -> httpx.Client:
    # Token comes from the vault via the platform credential broker, never from the profile.
    base_url = params.get("baseUrl", "https://jenkins.internal")
    token_ref = params.get("tokenRef", "vault://ath/jenkins")
    raise AdapterError(f"Jenkins client not configured (baseUrl={base_url}, tokenRef={token_ref})", retryable=False)


@registry.register("pipeline", "jenkins-job")
class JenkinsJob(PipelineAdapter):
    def trigger(self, ctx: RunContext, commit: str, extra: dict[str, Any] | None = None) -> ExecutionHandle:
        # TODO: POST /job/{job}/buildWithParameters with params + ATH_RUN_ID=ctx.run_id.
        # Idempotency: first look for an existing build tagged with this run id and reuse it.
        _client(self.params)
        raise NotImplementedError

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        # TODO: GET /job/{job}/{build}/api/json -> building/result
        raise NotImplementedError


@registry.register("executor", "jenkins-job")
class JenkinsSuiteExecutor(ExecutorAdapter):
    def start(self, suite: str, ctx: RunContext, commit: str) -> ExecutionHandle:
        raise NotImplementedError

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        raise NotImplementedError

    def collect(self, handle: ExecutionHandle) -> RawResults:
        # TODO: download archived artifacts matching results.path
        raise NotImplementedError
