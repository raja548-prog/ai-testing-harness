"""State store: run manifests, stage checkpoints, artifacts, baseline cache.

SQLite for local and desktop development; swap for Postgres in the ATH service
(same three tables). Workflow durability in production comes from the workflow
engine (e.g. Temporal); this store is still the source of truth for results.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ath_core.schemas.run import RunManifest, Stage, StageResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (run_id TEXT, stage TEXT, result TEXT, PRIMARY KEY (run_id, stage));
CREATE TABLE IF NOT EXISTS artifacts (run_id TEXT, name TEXT, body TEXT, PRIMARY KEY (run_id, name));
CREATE TABLE IF NOT EXISTS baselines (product TEXT, commit_sha TEXT, suite TEXT, atrf TEXT,
                                      known_good INTEGER DEFAULT 0, PRIMARY KEY (product, commit_sha, suite));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT,
                                   kind TEXT, detail TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
"""


class StateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(_SCHEMA)

    # runs ------------------------------------------------------------------
    def save_manifest(self, m: RunManifest) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)", (m.context.run_id, m.model_dump_json()))

    def load_manifest(self, run_id: str) -> RunManifest | None:
        row = self.db.execute("SELECT manifest FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunManifest.model_validate_json(row[0]) if row else None

    # checkpoints -------------------------------------------------------------
    def checkpoint(self, run_id: str, result: StageResult) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?)",
                            (run_id, result.stage.value, result.model_dump_json()))

    def get_checkpoint(self, run_id: str, stage: Stage) -> StageResult | None:
        row = self.db.execute("SELECT result FROM checkpoints WHERE run_id=? AND stage=?",
                              (run_id, stage.value)).fetchone()
        return StageResult.model_validate_json(row[0]) if row else None

    # artifacts ---------------------------------------------------------------
    def put_artifact(self, run_id: str, name: str, body: Any) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?)", (run_id, name, text))

    def get_artifact(self, run_id: str, name: str) -> str | None:
        row = self.db.execute("SELECT body FROM artifacts WHERE run_id=? AND name=?", (run_id, name)).fetchone()
        return row[0] if row else None

    # baseline cache ------------------------------------------------------------
    def get_baseline(self, product: str, commit: str, suite: str) -> str | None:
        row = self.db.execute("SELECT atrf FROM baselines WHERE product=? AND commit_sha=? AND suite=?",
                              (product, commit, suite)).fetchone()
        return row[0] if row else None

    def put_baseline(self, product: str, commit: str, suite: str, atrf_json: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO baselines (product, commit_sha, suite, atrf) VALUES (?, ?, ?, ?)",
                            (product, commit, suite, atrf_json))

    # structured events -----------------------------------------------------------
    def event(self, run_id: str, stage: str, kind: str, **detail: Any) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT INTO events (run_id, stage, kind, detail) VALUES (?, ?, ?, ?)",
                            (run_id, stage, kind, json.dumps(detail, default=str)))

    def events(self, run_id: str) -> list[tuple[str, str, dict]]:
        rows = self.db.execute("SELECT stage, kind, detail FROM events WHERE run_id=? ORDER BY id", (run_id,))
        return [(s, k, json.loads(d)) for s, k, d in rows]
