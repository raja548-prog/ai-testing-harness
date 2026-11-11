"""Product Test Profile: the onboarding contract (apiVersion ainxt.io/test-profile/v1).

The profile is declarative only. It names adapters and their parameters; it never
contains scripts. The core validates it before any run starts.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ath_core.masking import CATEGORIES as MASK_CATEGORIES

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

    @model_validator(mode="after")
    def _poll_sec_only_where_polled(self) -> "AdapterRef":
        if "pollSec" in (self.model_extra or {}):
            raise ValueError("pollSec only applies to deploy and suite executors, whose jobs ATH polls")
        return self


class PolledAdapterRef(AdapterRef):
    """An adapter whose jobs ATH polls for status: deploy and suite executors."""
    # Longest wait between status checks; polling starts faster and backs off up to this.
    poll_sec: float | None = Field(None, alias="pollSec", ge=1, le=600, strict=True)


class Repos(_Model):
    code: str
    tests: str | None = None


class HealthCheck(_Model):
    url: str
    expect: int = 200
    timeout_sec: int = Field(600, alias="timeoutSec")


class DeployConfig(PolledAdapterRef):
    health_check: HealthCheck | None = Field(None, alias="healthCheck")


class ResultsConfig(_Model):
    format: str
    path: str | None = None
    parser: str | None = None


class SuiteConfig(_Model):
    id: str
    executor: PolledAdapterRef
    results: ResultsConfig
    max_duration_min: int = Field(60, alias="maxDurationMin", ge=1)


class SelectionWhen(_Model):
    paths_changed: list[str] = Field(default_factory=list, alias="pathsChanged")


class SelectionRule(_Model):
    when: SelectionWhen
    run: list[str]


class Selection(_Model):
    default: list[str] = Field(min_length=1)
    rules: list[SelectionRule] = Field(default_factory=list)


class TestSource(_Model):
    """One kind of file in the test checkout that T3 may create or update.

    `kind: code` is test code (edited as text). `kind: data` holds test data or transactions, edited by
    format: csv/tsv by row operations, json/yaml by text edits that must still parse.
    Paths are globs relative to the test checkout (`**` spans folders)."""
    __test__ = False
    kind: Literal["code", "data"]
    format: Literal["text", "csv", "tsv", "json", "yaml"] | None = None
    paths: list[str] = Field(min_length=1)
    key_column: str | None = Field(None, alias="keyColumn")  # csv/tsv: the column `update_row` matches on

    @model_validator(mode="after")
    def _format_fits_kind(self) -> "TestSource":
        if self.kind == "code" and self.format not in (None, "text"):
            raise ValueError(f"a code source is edited as text; format {self.format!r} needs kind: data")
        if self.kind == "data" and self.format in (None, "text"):
            raise ValueError("a data source needs format: csv, tsv, json or yaml")
        if self.key_column and self.format not in ("csv", "tsv"):
            raise ValueError("keyColumn only applies to csv and tsv sources")
        for g in self.paths:
            if g.startswith("/") or ".." in Path(g).parts:
                raise ValueError(f"source path {g!r} must be relative to the test checkout, without '..'")
        return self

    @property
    def edit_format(self) -> str:
        return self.format or "text"


class AuthoringLimits(_Model):
    """Size caps for T3: what is sent to the model and what it may change."""
    max_changes: int = Field(20, alias="maxChanges", ge=1, le=200)               # files per proposal
    max_change_chars: int = Field(60000, alias="maxChangeChars", ge=100)         # content or edits per file
    test_files: int = Field(15, alias="testFiles", ge=1, le=200)                 # `tests` block: whole files
    tests_chars: int = Field(40000, alias="testsChars", ge=1000)
    tree_paths: int = Field(400, alias="treePaths", ge=10)
    tsd_chars: int = Field(20000, alias="tsdChars", ge=1000)
    conventions_chars: int = Field(8000, alias="conventionsChars", ge=500)
    product_chars: int = Field(8000, alias="productChars", ge=500)
    code_chars: int = Field(12000, alias="codeChars", ge=500)
    templates_chars: int = Field(8000, alias="templatesChars", ge=500)
    diff_chars: int = Field(8000, alias="diffChars", ge=500)
    outdated_chars: int = Field(6000, alias="outdatedChars", ge=500)


class TestAuthoring(_Model):
    """T3: create or update tests from the TSD. Only `local-fs` is built; the other modes validate,
    and T3 skips them. Relative paths: `conventions` from the profile file; `sources`, `testData` and
    `templates` from the test checkout; `codeContext` from the code checkout."""
    __test__ = False
    enabled: bool = True  # a run's --author / --no-author overrides it
    mode: Literal["local-fs", "git-mr", "test-server-api", "simulator-scenarios"]
    framework: str | None = None
    conventions: str | None = None
    sources: list[TestSource] = Field(default_factory=list)
    test_data: str | None = Field(None, alias="testData")              # approved test-data catalogue
    code_context: list[str] = Field(default_factory=list, alias="codeContext")
    templates: list[str] = Field(default_factory=list)                  # simulator template globs
    limits: AuthoringLimits = Field(default_factory=AuthoringLimits)

    @field_validator("templates", "code_context", mode="before")
    @classmethod
    def _one_or_many(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _sources_for_local_fs(self) -> "TestAuthoring":
        if self.mode == "local-fs" and not self.sources:
            raise ValueError("testAuthoring.mode local-fs needs sources: which test files T3 may change")
        return self


class LogsConfig(_Model):
    sources: list[AdapterRef] = Field(min_length=1)
    masking: list[str] = Field(default_factory=lambda: list(MASK_CATEGORIES))

    @model_validator(mode="after")
    def _keys_are_lists(self) -> "LogsConfig":
        for i, src in enumerate(self.sources):
            keys = src.params.get("keys")
            if keys is not None and not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
                # `keys: rrn` would otherwise become the keys r, r, n
                raise ValueError(f"logs.sources[{i}].keys must be a list of key names, e.g. [rrn, stan]; got {keys!r}")
        return self

    @field_validator("masking")
    @classmethod
    def _known_categories(cls, v: list[str]) -> list[str]:
        # A typo or `PAN` must not silently switch masking off.
        v = [c.strip().lower() for c in v]
        if unknown := sorted(set(v) - set(MASK_CATEGORIES)):
            raise ValueError(f"unknown masking categories {unknown}; known: {list(MASK_CATEGORIES)}")
        return v


class EnvLockConfig(AdapterRef):
    env: str
    wait_max_min: int = Field(120, alias="waitMaxMin")


class KnownFailure(_Model):
    case_id: str = Field(alias="caseId")
    ticket: str
    expires: str  # ISO date, e.g. 2026-12-31

    @field_validator("expires")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"expires must be an ISO date like 2026-12-31, got {v!r}")
        return v


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
    # Product skill shared by every LLM stage (path relative to this profile file).
    product_skill: str | None = Field(None, alias="productSkill")
    _source: Path | None = PrivateAttr(None)  # the file it was loaded from; not part of the profile hash

    @field_validator("api_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v not in SUPPORTED_API_VERSIONS:
            raise ValueError(f"unsupported apiVersion {v!r}; supported: {sorted(SUPPORTED_API_VERSIONS)}")
        return v

    @property
    def source(self) -> Path | None:
        return self._source

    def resolve_path(self, rel: str | Path) -> Path:
        """A path from the profile, relative to the profile file (to the working directory if unknown)."""
        p = Path(rel)
        if p.is_absolute() or self._source is None:
            return p
        return self._source.parent / p

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
    profile._source = Path(path)
    problems = profile.check_references()
    if problems:
        raise ValueError("invalid test profile: " + "; ".join(problems))
    return profile
