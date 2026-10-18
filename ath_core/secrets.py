"""Resolve secret references from profiles. Profiles name a secret; they never contain one.

Supported today:  env:NAME   -> value of environment variable NAME
Reserved:         vault:PATH -> resolved by the platform credential broker (not wired yet)
"""
from __future__ import annotations

import os

from ath_core.adapters.base import AdapterError


def resolve_secret(ref: str | None, what: str) -> str:
    if not ref:
        raise AdapterError(f"{what}: no secret reference configured", retryable=False)
    scheme, _, name = ref.partition(":")
    if scheme == "env" and name:
        value = os.environ.get(name)
        if not value:
            raise AdapterError(f"{what}: environment variable {name} is not set", retryable=False)
        return value
    if scheme == "vault":
        raise AdapterError(f"{what}: vault references need the platform credential broker, "
                           f"which is not available yet; use env:NAME for now", retryable=False)
    raise AdapterError(f"{what}: unsupported secret reference {ref!r}; use env:NAME", retryable=False)


def secret_problem(ref: str | None, what: str) -> str | None:
    """Like resolve_secret, but returns the problem instead of raising (for doctor)."""
    try:
        resolve_secret(ref, what)
    except AdapterError as e:
        return str(e)
    return None
