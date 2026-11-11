"""Test authoring into a local test checkout (`testAuthoring.mode: local-fs`).

Writes an approved proposal into the checkout on this machine. All files are checked first, then
written: if one file changed since the proposal (its SHA-256 differs from the base the model saw) or
no longer passes the guards, nothing is written. Catalogue values are filled in only here, so they
appear in the written files and nowhere else. A file already in its proposed state is left alone, so
applying the same proposal twice (e.g. a resume after a crash) is harmless.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ath_core.adapters.base import AdapterError, TestAuthor, registry
from ath_core.authoring.engine import Workspace
from ath_core.authoring.formats import GuardError, sha256_bytes
from ath_core.schemas.authoring import AppliedResult, Proposal


def _write(target: Path, data: bytes) -> None:
    """Replace the file in one step, keeping its permissions."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ath-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@registry.register("author", "local-fs")
class LocalFsAuthor(TestAuthor):
    def __init__(self, profile: Any = None, **params: Any):
        super().__init__(**params)
        if profile is None:
            raise AdapterError("local-fs test author needs the product profile", retryable=False)
        self.profile = profile

    def apply(self, proposal: Proposal, base_dir: Path | None, approved_by: str) -> AppliedResult:
        if base_dir is None:
            raise AdapterError("local-fs test author needs the test checkout folder", retryable=False)
        try:
            ws = Workspace(self.profile, base_dir)
        except ValueError as e:
            raise AdapterError(str(e), retryable=False)
        writes: list[tuple[str, Path, bytes]] = []
        already: list[str] = []
        for ch in proposal.changes:
            def refuse(why: str) -> AdapterError:
                return AdapterError(f"{ch.path}: {why}; nothing was written", retryable=False)

            try:
                target, source = ws.check_path(ch.path)
            except GuardError as e:
                raise refuse(str(e))
            if (source.kind, source.edit_format) != (ch.kind, ch.format):
                raise refuse(f"the profile now treats this file as {source.kind}/{source.edit_format}, "
                             f"not {ch.kind}/{ch.format}")
            current = target.read_bytes() if target.is_file() else None
            if current is not None and ch.result_sha256 and sha256_bytes(current) == ch.result_sha256:
                already.append(ch.path)
                continue
            if ch.action == "update" and (current is None or sha256_bytes(current) != ch.base_sha256):
                raise refuse("the file changed after the proposal was made")
            if ch.action == "create" and (current is not None or target.is_symlink()):
                raise refuse("the file exists now")
            try:
                realized = ws.realize(ch, editable=None)
            except (GuardError, KeyError) as e:
                raise refuse(str(e))
            writes.append((ch.path, target, realized.new_bytes))
        for _, target, data in writes:
            _write(target, data)
        return AppliedResult(applied=[p for p, _, _ in writes], already_applied=already,
                             proposal_hash=proposal.hash, approved_by=approved_by)
