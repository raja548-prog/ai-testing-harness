"""Every shipped asset (skills, model policy, templates) is declared as a data file in pyproject.toml.

Data files are copied flat per target folder, so a new skill folder needs its own entry; without one,
an installed ATH would silently lack that skill.
"""
import tomllib
from pathlib import Path

from tests.conftest import ROOT

ASSET_DIRS = ("skills", "config", "templates")


def _shipped() -> dict[str, str]:
    """Repo-relative file -> its install folder under share/ainxt-ath/, per pyproject.toml."""
    data_files = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["data-files"]
    out = {}
    for target, globs in data_files.items():
        for pattern in globs:
            for path in ROOT.glob(pattern):
                out[path.relative_to(ROOT).as_posix()] = target
    return out


def test_every_asset_is_packaged_in_its_own_folder():
    shipped = _shipped()
    for top in ASSET_DIRS:
        for path in (ROOT / top).rglob("*"):
            if path.is_dir() or path.name.startswith("."):
                continue
            rel = path.relative_to(ROOT).as_posix()
            assert rel in shipped, f"{rel} is not listed under [tool.setuptools.data-files]"
            assert shipped[rel] == f"share/ainxt-ath/{Path(rel).parent.as_posix()}", f"{rel} installs to {shipped[rel]}"


def test_data_file_globs_match_something():
    data_files = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["data-files"]
    for target, globs in data_files.items():
        for pattern in globs:
            assert list(ROOT.glob(pattern)), f"{target}: {pattern} matches no files"
