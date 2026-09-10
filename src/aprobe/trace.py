"""Trace 持久化。

边界（ADR-0002）：这里是权威事实来源。编排层的中间状态不参与回放、
评估与结论推导；模型名、提示版本、工具调用与预算消耗都必须显式落库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import TestRun

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL,
    operation_id       TEXT NOT NULL,
    target             TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    duration_ms        INTEGER NOT NULL,
    verdict            TEXT NOT NULL,
    termination_reason TEXT NOT NULL,
    spec_source        TEXT NOT NULL DEFAULT '',
    spec_version       TEXT NOT NULL DEFAULT '',
    aprobe_version     TEXT NOT NULL DEFAULT '',
    planner_version    TEXT NOT NULL DEFAULT '',
    payload            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_case_id ON runs(case_id);
"""


class TraceStore:
    """一次运行的执行事实。用例文件与它互不覆盖：前者是意图，后者是事实。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, run: TestRun) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, case_id, operation_id, target, started_at, duration_ms,
                    verdict, termination_reason, spec_source, spec_version,
                    aprobe_version, planner_version, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.case_id,
                    run.operation_id,
                    run.target,
                    run.started_at.isoformat(),
                    run.duration_ms,
                    run.verdict.value,
                    run.termination_reason.value,
                    run.spec_source,
                    run.spec_version,
                    run.aprobe_version,
                    run.planner_version,
                    run.model_dump_json(),
                ),
            )

    def get(self, run_id: str) -> TestRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return TestRun.model_validate_json(row["payload"]) if row else None

    def list_runs(self, limit: int | None = None) -> list[TestRun]:
        query = "SELECT payload FROM runs ORDER BY started_at ASC, run_id ASC"
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [TestRun.model_validate_json(row["payload"]) for row in rows]

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) AS total FROM runs").fetchone()["total"])
