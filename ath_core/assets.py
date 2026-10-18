"""Locate the files ATH ships next to its code: stage skills, the model policy, onboarding templates.

In a source checkout they are the repo's top-level skills/, config/ and templates/ folders. In an
installed wheel they are data files under <prefix>/share/ainxt-ath/ (see pyproject.toml).
"""
from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

SHARE = Path("share") / "ainxt-ath"


def asset_path(relative: str) -> Path:
    """First existing location of an asset; the checkout path if none exists, so errors name it."""
    checkout = Path(__file__).resolve().parents[1] / relative
    candidates = [checkout, Path(sysconfig.get_path("data")) / SHARE / relative, Path(sys.prefix) / SHARE / relative]
    for path in candidates:
        if path.exists():
            return path
    return checkout
