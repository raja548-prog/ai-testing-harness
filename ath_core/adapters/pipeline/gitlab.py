"""GitLab CI pipeline adapter (skeleton: wire to the GitLab pipelines API)."""
from __future__ import annotations

from typing import Any

from ath_core.adapters.base import ExecStatus, ExecutionHandle, PipelineAdapter, registry
from ath_core.schemas.run import RunContext


@registry.register("pipeline", "gitlab-pipeline")
class GitLabPipeline(PipelineAdapter):
    def trigger(self, ctx: RunContext, commit: str, extra: dict[str, Any] | None = None) -> ExecutionHandle:
        # TODO: POST /projects/:id/pipeline?ref=<branch> with variables ATH_RUN_ID, COMMIT_ID
        raise NotImplementedError

    def poll(self, handle: ExecutionHandle) -> ExecStatus:
        # TODO: GET /projects/:id/pipelines/:pipeline_id -> status
        raise NotImplementedError
