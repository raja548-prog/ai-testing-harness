"""Model policy: which models may see which data. Deterministic, no router model, fail closed.

Every LLM stage sees a class of data (masked logs and results are still `restricted`: masking
is regex-based and can miss things). The policy lists, per data class, the model-name globs
allowed to receive it. A run whose configured model is not allowed for a stage is refused.

The policy file is platform-owned (security review), not product-owned: a product cannot opt
its own data into cloud models. Per-product exceptions go in `productOverrides`, in this file.

  dataClasses:      { restricted: { allowed: ["local/*"] }, internal: {...}, public: {...} }
  stages:           { triage: restricted, impact: internal, ... }   # unknown stage -> restricted
  productOverrides: { settlenxt: { impact: public } }
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ath_core.assets import asset_path

DEFAULT_POLICY = asset_path("config/model-policy.yaml")
FALLBACK_CLASS = "restricted"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DataClassRule(_Model):
    allowed: list[str] = Field(default_factory=list)  # model-name globs; empty = no model may see it


class ModelPolicy(_Model):
    data_classes: dict[str, DataClassRule] = Field(alias="dataClasses")
    stages: dict[str, str] = Field(default_factory=dict)
    product_overrides: dict[str, dict[str, str]] = Field(default_factory=dict, alias="productOverrides")
    source: Path | None = Field(None, exclude=True)

    @model_validator(mode="after")
    def _classes_exist(self) -> "ModelPolicy":
        if FALLBACK_CLASS not in self.data_classes:
            raise ValueError(f"model policy must define the {FALLBACK_CLASS!r} data class")
        used = set(self.stages.values()) | {c for o in self.product_overrides.values() for c in o.values()}
        if unknown := sorted(used - set(self.data_classes)):
            raise ValueError(f"model policy uses undefined data classes: {unknown}")
        return self

    def data_class(self, stage: str, product: str) -> str:
        override = self.product_overrides.get(product, {}).get(stage)
        return override or self.stages.get(stage, FALLBACK_CLASS)

    def allows(self, model: str, stage: str, product: str) -> bool:
        rule = self.data_classes[self.data_class(stage, product)]
        return bool(model) and any(fnmatch.fnmatchcase(model, g) for g in rule.allowed)

    def problem(self, models: list[str], stage: str, product: str) -> str | None:
        """Why these models may not run this stage for this product, or None."""
        denied = sorted({m or "(no model)" for m in models if not self.allows(m, stage, product)})
        if not denied:
            return None
        cls = self.data_class(stage, product)
        return (f"model policy: stage {stage!r} sees {cls!r} data for {product!r}, which only "
                f"{self.data_classes[cls].allowed or 'no models'} may receive; not allowed: {denied}")


def load_policy(path: str | Path | None = None) -> ModelPolicy:
    """Load the policy. A missing or invalid file is an error: there is no permissive default."""
    p = Path(path) if path else DEFAULT_POLICY
    if not p.exists():
        raise ValueError(f"model policy {p} not found; LLM stages cannot run without one")
    policy = ModelPolicy.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    policy.source = p
    return policy
