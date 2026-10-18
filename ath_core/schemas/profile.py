"""Product Test Profile: the onboarding contract (apiVersion ainxt.io/test-profile/v1).

The profile is declarative only. It names adapters and their parameters; it never
contains scripts. The core validates it before any run starts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_API_VERSIONS = {"ainxt.io/test-profile/v1"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AdapterRef(BaseModel):
    """Reference to a registered adapter plus its free-form parameters."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    adapter: str

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class Repos(_Model):
    code: str
    tests: str | None = None


class HealthCheck(_Model):
    url: str
    expect: int = 200
    timeout_sec: int = Field(600, alias="timeoutSec")


class DeployConfig(AdapterRef):
    health_check: HealthCheck | None = Field(None, alias="healthCheck")


class ResultsConfig(_Model):
    format: str
    path: str | None = None
    parser: str | None = None


class SuiteConfig(_Model):
    id: str
    executor: AdapterRef
    results: ResultsConfig
    max_duration_min: int = Field(60, alias="maxDurationMin")


class SelectionWhen(_Model):
    paths_changed: list[str] = Field(default_factory=list, alias="pathsChanged")


class SelectionRule(_Model):
    when: SelectionWhen
    run: list[str]


class Selection(_Model):
    default: list[str]
    rules: list[SelectionRule] = Field(default_factory=list)


class TestAuthoring(_Model):
    __test__ = False
    mode: Literal["git-mr", "test-server-api", "simulator-scenarios"]
    framework: str | None = None
    conventions: str | None = None


class LogsConfig(_Model):
    sources: list[AdapterRef] = Field(min_length=1)
    masking: list[str] = Field(default_factory=lambda: ["pan", "cvv", "track2", "aadhaar", "mobile"])


class EnvLockConfig(AdapterRef):
    env: str
    wait_max_min: int = Field(120, alias="waitMaxMin")


class KnownFailure(_Model):
    case_id: str = Field(alias="caseId")
    ticket: str
    expires: str  # ISO date, e.g. 2026-12-31


class Policy(_Model):
    flaky_retries: int = Field(2, alias="flakyRetries", ge=0, le=5)
    quarantine_list: str | None = Field(None, alias="quarantineList")
    max_fix_iterations: int = Field(3, alias="maxFixIterations", ge=0, le=10)
    environments: list[str] = Field(min_length=1)
    env_lock: EnvLockConfig | None = Field(None, alias="envLock")
    known_failures: list[KnownFailure] = Field(default_factory=list, alias="knownFailures")


class Models(BaseModel):
    """Model per stage role; any extra key (triage, remediate, ...) overrides `default`."""
    model_config = ConfigDict(extra="allow")
    default: str

    def for_stage(self, role: str) -> str:
        return (self.model_extra or {}).get(role, self.default)


class TestProfile(_Model):
    __test__ = False  # stop pytest collecting this class

    api_version: str = Field(alias="apiVersion")
    product: str
    owners: list[str] = Field(min_length=1)
    repos: Repos
    build: AdapterRef
    deploy: DeployConfig
    suites: list[SuiteConfig] = Field(min_length=1)
    selection: Selection
    test_authoring: TestAuthoring | None = Field(None, alias="testAuthoring")
    logs: LogsConfig
    policy: Policy
    models: Models | None = None

    @field_validator("api_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v not in SUPPORTED_API_VERSIONS:
            raise ValueError(f"unsupported apiVersion {v!r}; supported: {sorted(SUPPORTED_API_VERSIONS)}")
        return v

    def correlation_keys(self) -> list[str]:
        """Correlation keys the product declares on its log sources (e.g. rrn, stan, batchId)."""
        keys: list[str] = []
        for src in self.logs.sources:
            keys += [k for k in src.params.get("keys") or [] if isinstance(k, str) and k not in keys]
        return keys

    def suite(self, suite_id: str) -> SuiteConfig:
        for s in self.suites:
            if s.id == suite_id:
                return s
        raise KeyError(f"suite {suite_id!r} not in profile {self.product}")

    def check_references(self) -> list[str]:
        """Cross-field checks that field validation cannot express. Returns problems found."""
        problems: list[str] = []
        ids = [s.id for s in self.suites]
        if len(set(ids)) != len(ids):
            problems.append("duplicate suite ids")
        for sid in self.selection.default:
            if sid not in ids:
                problems.append(f"selection.default references unknown suite {sid!r}")
        for i, rule in enumerate(self.selection.rules):
            for sid in rule.run:
                if sid not in ids:
                    problems.append(f"selection.rules[{i}] references unknown suite {sid!r}")
        lock = self.policy.env_lock
        if lock and lock.env not in self.policy.environments:
            problems.append(f"envLock env {lock.env!r} is not in policy.environments")
        return problems


def load_profile(path: str | Path) -> TestProfile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    profile = TestProfile.model_validate(data)
    problems = profile.check_references()
    if problems:
        raise ValueError("invalid test profile: " + "; ".join(problems))
    return profile
