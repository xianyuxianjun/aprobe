"""离线评估：把一组标注过的样例折成可比较的指标。

边界：
- 本模块**不做任何 I/O**。它只对 `run_once()` 交回来的 TestRun 做计算，
  因此可以被纯函数式地测试，也不需要 mock 任何东西。
- 指标只在**声明过的基准**上才有意义，所以 `scenario` 必须与实际目标一致；
  这一检查由调用方在跑之前完成（`runner.probe_baseline`）。
- 评估通过不等于目标安全/正确；它只说明确定性管线与标注一致。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError
from .models import TestRun, Verdict

SUITE_VERSION = 1


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    verdict: Verdict
    note: str = ""


class EvalSuite(BaseModel):
    """一份带标注的离线样例集。它进 Git，是回放的权威来源。"""

    model_config = ConfigDict(extra="forbid")

    version: int = SUITE_VERSION
    name: str
    scenario: str
    spec: str
    cases: str
    expectations: list[Expectation] = Field(default_factory=list)

    def expected_for(self, case_id: str) -> Verdict | None:
        for expectation in self.expectations:
            if expectation.case_id == case_id:
                return expectation.verdict
        return None


class CaseOutcome(BaseModel):
    case_id: str
    expected: Verdict
    actual: Verdict
    matched: bool
    termination_reason: str
    evidence: int
    duration_ms: int


class Metrics(BaseModel):
    total: int
    matched: int
    accuracy: float
    false_positives: int
    false_negatives: int
    inconclusive: int
    unsupported_conclusions: int
    evidence_coverage: float
    consistency: float
    total_duration_ms: int
    mean_duration_ms: int


class EvaluationReport(BaseModel):
    suite: str
    scenario: str
    scenario_version: str
    repeat: int
    metrics: Metrics
    outcomes: list[CaseOutcome] = Field(default_factory=list)
    agent_steps: int = 0
    agent_tokens: int = 0
    declared_by: str = ""
    notes: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def load_suite(path: str | Path) -> EvalSuite:
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"评估样例集不存在: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"评估样例集不是合法 YAML: {source}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"评估样例集顶层必须是对象: {source}")
    try:
        return EvalSuite.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"评估样例集结构非法: {source}\n{exc}") from exc


def evaluate_suite(
    suite: EvalSuite,
    *,
    run_once: Callable[[], list[TestRun]],
    scenario_version: str = "",
    repeat: int = 1,
    declared_by: str = "",
    agent_steps: int = 0,
    agent_tokens: int = 0,
) -> EvaluationReport:
    """重复执行 run_once()，把每次的结论与标注对比。

    重复的意义是回答"同一组用例重复回放，结论是否一致"——这是"可复现"的可执行形式。
    """
    rounds: list[dict[str, TestRun]] = []
    for _ in range(max(1, repeat)):
        rounds.append({run.case_id: run for run in run_once()})

    first = rounds[0]
    outcomes: list[CaseOutcome] = []
    unsupported = 0
    for case_id in sorted(first):
        run = first[case_id]
        expected = suite.expected_for(case_id)
        if expected is None:
            continue
        evidence = len(run.assertion_results)
        if run.verdict in (Verdict.PASSED, Verdict.FAILED) and evidence == 0:
            # 没有断言就给出通过/失败，就是"无依据结论"。结构上不该发生，所以要盯住它。
            unsupported += 1
        outcomes.append(
            CaseOutcome(
                case_id=case_id,
                expected=expected,
                actual=run.verdict,
                matched=expected is run.verdict,
                termination_reason=run.termination_reason.value,
                evidence=evidence,
                duration_ms=run.duration_ms,
            )
        )

    total = len(outcomes)
    matched = sum(1 for outcome in outcomes if outcome.matched)
    false_positives = sum(1 for outcome in outcomes if outcome.expected is Verdict.PASSED and outcome.actual is Verdict.FAILED)
    false_negatives = sum(1 for outcome in outcomes if outcome.expected is Verdict.FAILED and outcome.actual is Verdict.PASSED)
    inconclusive = sum(1 for outcome in outcomes if outcome.actual is Verdict.INCONCLUSIVE)
    durations = [outcome.duration_ms for outcome in outcomes]

    metrics = Metrics(
        total=total,
        matched=matched,
        accuracy=(matched / total) if total else 0.0,
        false_positives=false_positives,
        false_negatives=false_negatives,
        inconclusive=inconclusive,
        unsupported_conclusions=unsupported,
        evidence_coverage=(1 - unsupported / total) if total else 0.0,
        consistency=_consistency(rounds),
        total_duration_ms=sum(durations),
        mean_duration_ms=int(sum(durations) / total) if total else 0,
    )
    notes: list[str] = []
    missing = sorted(set(first) - {item.case_id for item in suite.expectations})
    if missing:
        notes.append(f"以下用例没有标注预期结论，未计入指标：{', '.join(missing)}")
    unrun = sorted({item.case_id for item in suite.expectations} - set(first))
    if unrun:
        notes.append(f"标注中有未执行的用例：{', '.join(unrun)}")

    return EvaluationReport(
        suite=suite.name,
        scenario=suite.scenario,
        scenario_version=scenario_version,
        repeat=max(1, repeat),
        metrics=metrics,
        outcomes=outcomes,
        agent_steps=agent_steps,
        agent_tokens=agent_tokens,
        declared_by=declared_by,
        notes=notes,
    )


def _consistency(rounds: list[dict[str, TestRun]]) -> float:
    """同一用例在多轮之间结论一致的占比。只有一轮时无歧义，记为 1。"""
    if len(rounds) <= 1:
        return 1.0
    case_ids = sorted(rounds[0])
    if not case_ids:
        return 0.0
    agreeing = sum(
        1 for case_id in case_ids if len({round_[case_id].verdict for round_ in rounds if case_id in round_}) == 1
    )
    return agreeing / len(case_ids)


def render_report(report: EvaluationReport) -> str:
    metrics = report.metrics
    lines = [
        f"# 评估报告：{report.suite}",
        "",
        f"- 基准：`{report.scenario}`（版本 {report.scenario_version or '未确认'}）",
        f"- 规划器：{report.declared_by or '未记录'}",
        f"- 回放轮数：{report.repeat}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 覆盖用例 | {metrics.total} |",
        f"| 与标注一致 | {metrics.matched} |",
        f"| 判定准确率 | {metrics.accuracy:.1%} |",
        f"| 假阳性（应为通过，实际失败） | {metrics.false_positives} |",
        f"| 假阴性（应为失败，实际通过） | {metrics.false_negatives} |",
        f"| 无法判定 | {metrics.inconclusive} |",
        f"| 无依据结论 | {metrics.unsupported_conclusions} |",
        f"| 证据覆盖率 | {metrics.evidence_coverage:.1%} |",
        f"| 回放一致率 | {metrics.consistency:.1%} |",
        f"| 平均耗时 | {metrics.mean_duration_ms}ms |",
        f"| Agent 步数 / token | {report.agent_steps} / {report.agent_tokens} |",
        "",
        "## 逐条对比",
        "",
        "| 用例 | 标注 | 实际 | 一致 | 终止原因 | 断言数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in report.outcomes:
        mark = "✓" if outcome.matched else "✗"
        lines.append(
            f"| `{outcome.case_id}` | {outcome.expected.value} | {outcome.actual.value} "
            f"| {mark} | {outcome.termination_reason} | {outcome.evidence} |"
        )
    for note in report.notes:
        lines += ["", f"> {note}"]
    lines += [
        "",
        "## 声明",
        "",
        "这些数字只描述「当前版本的管线在上述基准与上述标注上的表现」，",
        "不构成对任意接口的准确率声明，也不等于被测接口没有问题。",
        "标注本身是人写的，标注错了，指标也会跟着错。",
        "",
    ]
    return "\n".join(lines)


def report_payload(report: EvaluationReport) -> dict[str, Any]:
    return report.model_dump(mode="json")
