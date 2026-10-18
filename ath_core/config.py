"""Harness config: settings that belong to where ATH runs (desktop or server), not to a product.

Product behaviour lives in the approved Test Profile. This file only says how to reach local
tools, e.g. which agent CLI runs the LLM stages. It never contains secrets.

Lookup order (first file found wins):
  1. --config PATH
  2. $ATH_CONFIG
  3. ./.ath/config.yaml          (project)
  4. ~/.config/ainxt/ath.yaml    (user)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ath_core.assets import asset_path

DEFAULT_SKILLS_DIR = asset_path("skills")
# Flags that make an agent CLI approve every tool call. ATH never runs a session with them:
# a prompt-injected log line could otherwise run commands unchallenged.
AUTO_APPROVE_FLAGS = ("--yolo", "--always-approve", "bypassPermissions", "--dangerously-skip-permissions")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AgentRunnerConfig(_Model):
    """How to run one headless agent CLI session (AiNxt CLI, or any grok-cli style tool).

    command[0] is the CLI: a name on PATH or a path (~ and $VARS are expanded).
    Placeholders in the other arguments: {prompt} {prompt_file} {model} {workdir}
    """
    command: list[str] = Field(min_length=1)
    prompt_via: Literal["arg", "stdin", "file"] = Field("arg", alias="promptVia")
    timeout_sec: int = Field(300, alias="timeoutSec", ge=10, le=3600)
    pass_env: list[str] = Field(default_factory=list, alias="passEnv")
    set_env: dict[str, str] = Field(default_factory=dict, alias="setEnv")  # fixed values, e.g. AINXT_MAX_RETRIES
    voters: int = Field(3, ge=1, le=5)
    models: list[str] = Field(default_factory=list)  # one per voter, cycled; else the profile's triage model
    # Model family per glob, e.g. {"claude-*": anthropic, "onprem-llama*": meta}. Voters from one family
    # make correlated mistakes; minFamilies > 1 requires that many distinct families among the voters.
    model_families: dict[str, str] = Field(default_factory=dict, alias="modelFamilies")
    min_families: int = Field(1, alias="minFamilies", ge=1, le=5)

    @field_validator("command")
    @classmethod
    def _expand_executable(cls, v: list[str]) -> list[str]:
        # The CLI runs without a shell, so expand ~ and $VARS in its path here, e.g. ~/.ainxt/bin/ainxt.
        return [os.path.expandvars(os.path.expanduser(v[0])), *v[1:]] if v else v

    @model_validator(mode="after")
    def _placeholder_matches_prompt_via(self) -> "AgentRunnerConfig":
        joined = " ".join(self.command)
        auto_approve = [f for f in AUTO_APPROVE_FLAGS if any(f in arg for arg in self.command)]
        if auto_approve:
            raise ValueError(f"agent CLI must not auto-approve tool calls; remove {auto_approve} from command")
        if self.min_families > self.voters:
            raise ValueError(f"minFamilies ({self.min_families}) cannot exceed voters ({self.voters})")
        if self.prompt_via == "arg" and "{prompt}" not in joined:
            raise ValueError("promptVia: arg needs a {prompt} placeholder in command")
        if self.prompt_via == "file" and "{prompt_file}" not in joined:
            raise ValueError("promptVia: file needs a {prompt_file} placeholder in command")
        return self


class HarnessConfig(_Model):
    agent_runner: AgentRunnerConfig | None = Field(None, alias="agentRunner")
    skills_dir: Path = Field(DEFAULT_SKILLS_DIR, alias="skillsDir")
    model_policy: Path | None = Field(None, alias="modelPolicy")  # default: config/model-policy.yaml of the install
    source: Path | None = Field(None, exclude=True)


def config_candidates(explicit: str | Path | None = None) -> list[Path]:
    paths = [Path(explicit)] if explicit else []
    if os.environ.get("ATH_CONFIG"):
        paths.append(Path(os.environ["ATH_CONFIG"]))
    paths += [Path(".ath/config.yaml"), Path.home() / ".config/ainxt/ath.yaml"]
    return paths


def load_config(explicit: str | Path | None = None) -> HarnessConfig:
    """Load the first config file found; an explicit path must exist. No file means defaults."""
    if explicit and not Path(explicit).exists():
        raise FileNotFoundError(f"config file {explicit} not found")
    for path in config_candidates(explicit):
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cfg = HarnessConfig.model_validate(data)
            if not cfg.skills_dir.is_absolute():
                cfg.skills_dir = (path.parent / cfg.skills_dir).resolve()
            if cfg.model_policy and not cfg.model_policy.is_absolute():
                cfg.model_policy = (path.parent / cfg.model_policy).resolve()
            cfg.source = path
            return cfg
    return HarnessConfig()
