"""Trace 持久化。

边界（ADR-0002）：这里是权威事实来源。编排层的中间状态不参与回放、
评估与结论推导；模型名、提示版本、工具调用与预算消耗都必须显式落库。

两张表对应两类事实：
- `runs`：测出了什么（TestRun）；
- `agent_runs`：怎么想出来的（AgentRun，含每一步与每次工具调用）。
降级模式只写前者。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import AgentRun, FailureAttribution, TestRun

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
    spec_title         TEXT NOT NULL DEFAULT '',
    spec_version       TEXT NOT NULL DEFAULT '',
    cases_file         TEXT NOT NULL DEFAULT '',
    aprobe_version     TEXT NOT NULL DEFAULT '',
    planner            TEXT NOT NULL DEFAULT '',
    payload            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_case_id ON runs(case_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id             TEXT PRIMARY KEY,
    mode               TEXT NOT NULL,
    model              TEXT NOT NULL DEFAULT '',
    prompt_version     TEXT NOT NULL DEFAULT '',
    termination_reason TEXT NOT NULL,
    steps              INTEGER NOT NULL,
    tokens             INTEGER NOT NULL,
    started_at         TEXT NOT NULL,
    payload            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);

CREATE TABLE IF NOT EXISTS attributions (
    run_id      TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    category    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attributions_case_id ON attributions(case_id);
"""


class TraceStore:
    """用例文件与它互不覆盖：前者是意图，这里存的是事实。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    # ---- TestRun：测出了什么 ----

    def record(self, run: TestRun) -> None:
        provenance = run.provenance
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, case_id, operation_id, target, started_at, duration_ms,
                    verdict, termination_reason, spec_source, spec_title, spec_version,
                    cases_file, aprobe_version, planner, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.case_id,
                    run.operation_id,
                    provenance.target,
                    run.started_at.isoformat(),
                    run.duration_ms,
                    run.verdict.value,
                    run.termination_reason.value,
                    provenance.spec_source,
                    provenance.spec_title,
                    provenance.spec_version,
                    provenance.cases_file,
                    provenance.aprobe_version,
                    provenance.planner,
                    run.model_dump_json(),
                ),
            )

    def get(self, run_id: str) -> TestRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return TestRun.model_validate_json(row["payload"]) if row else None

    def list_runs(self, limit: int | None = None) -> list[TestRun]:
        return [
            TestRun.model_validate_json(row["payload"])
            for row in self._select("runs", limit)
        ]

    # ---- 归因：失败了是为什么（模型建议，不是 Verdict） ----

    def record_attribution(self, attribution: FailureAttribution) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO attributions (
                    run_id, case_id, operation_id, category, created_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attribution.run_id,
                    attribution.case_id,
                    attribution.operation_id,
                    attribution.category.value,
                    attribution.created_at.isoformat(),
                    attribution.model_dump_json(),
                ),
            )

    def get_attribution(self, run_id: str) -> FailureAttribution | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM attributions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return FailureAttribution.model_validate_json(row["payload"]) if row else None

    def list_attributions(self, limit: int | None = None) -> list[FailureAttribution]:
        return [
            FailureAttribution.model_validate_json(row["payload"])
            for row in self._select("attributions", limit, order_by="created_at")
        ]

    # ---- AgentRun：怎么想出来的 ----

    def record_agent_run(self, agent_run: AgentRun) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO agent_runs (
                    run_id, mode, model, prompt_version, termination_reason,
                    steps, tokens, started_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_run.run_id,
                    agent_run.mode,
                    agent_run.model,
                    agent_run.prompt_version,
                    agent_run.termination_reason.value,
                    agent_run.consumed_steps,
                    agent_run.consumed_tokens,
                    agent_run.started_at.isoformat(),
                    agent_run.model_dump_json(),
                ),
            )

    def get_agent_run(self, run_id: str) -> AgentRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return AgentRun.model_validate_json(row["payload"]) if row else None

    def list_agent_runs(self, limit: int | None = None) -> list[AgentRun]:
        return [
            AgentRun.model_validate_json(row["payload"])
            for row in self._select("agent_runs", limit)
        ]

    def _select(self, table: str, limit: int | None, order_by: str = "started_at") -> list[sqlite3.Row]:
        query = f"SELECT payload FROM {table} ORDER BY {order_by} ASC, run_id ASC"
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._connect() as connection:
            return connection.execute(query, params).fetchall()

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) AS total FROM runs").fetchone()["total"])

    def count_agent_runs(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) AS total FROM agent_runs").fetchone()["total"])
