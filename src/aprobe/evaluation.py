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

from .errors import ConfigError, ExitCode
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
    """一次回放的度量。

    注意 `accuracy` 的分母是**被覆盖到的标注数**，因此不同覆盖数的两次回放不可直接比。
    跨用例集可比的是 `defect_detection_rate`：它固定以样例集里**全部注入的违约**为分母。
    """

    total: int
    matched: int
    accuracy: float
    #: 样例集里的全部标注数（无论有没有被用例覆盖）
    labelled: int = 0
    #: 没有被任何用例覆盖到的标注数
    uncovered: int = 0
    #: 样例集里标注为失败的条数，即"注入了多少违约"
    total_defects: int = 0
    #: 其中被真正判为失败的数量。未被覆盖或误判为通过都不算发现。
    detected_defects: int = 0
    defect_detection_rate: float = 1.0
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
    covered_ids = set(first)
    detected_defects = 0
    for expectation in suite.expectations:
        if expectation.verdict is not Verdict.FAILED:
            continue
        run = first.get(expectation.case_id)
        if run is not None and run.verdict is Verdict.FAILED:
            detected_defects += 1
    total_defects = sum(1 for item in suite.expectations if item.verdict is Verdict.FAILED)
    matched = sum(1 for outcome in outcomes if outcome.matched)
    false_positives = sum(1 for outcome in outcomes if outcome.expected is Verdict.PASSED and outcome.actual is Verdict.FAILED)
    false_negatives = sum(1 for outcome in outcomes if outcome.expected is Verdict.FAILED and outcome.actual is Verdict.PASSED)
    inconclusive = sum(1 for outcome in outcomes if outcome.actual is Verdict.INCONCLUSIVE)
    durations = [outcome.duration_ms for outcome in outcomes]

    metrics = Metrics(
        total=total,
        matched=matched,
        accuracy=(matched / total) if total else 0.0,
        labelled=len(suite.expectations),
        uncovered=len(suite.expectations) - len(covered_ids & {item.case_id for item in suite.expectations}),
        total_defects=total_defects,
        detected_defects=detected_defects,
        defect_detection_rate=(detected_defects / total_defects) if total_defects else 1.0,
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
        notes.append(
            f"以下标注没有被任何用例覆盖，因此不可能被发现（{len(unrun)} 条）：{', '.join(unrun)}"
        )

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


class Comparison(BaseModel):
    """两套用例在同一基准、同一套标注下的对比。

    只有 `defect_detection_rate` 是可以直接相减的：它的分母是固定的标注集合。
    准确率不行——分母是各自的覆盖数。
    """

    left_label: str
    right_label: str
    left: Metrics
    right: Metrics
    detection_delta: float
    false_negative_delta: int
    coverage_delta: int
    duration_delta_ms: int
    agent_steps: int = 0
    agent_tokens: int = 0


def compare(left: EvaluationReport, right: EvaluationReport) -> Comparison:
    return Comparison(
        left_label=left.declared_by or left.suite,
        right_label=right.declared_by or right.suite,
        left=left.metrics,
        right=right.metrics,
        detection_delta=right.metrics.defect_detection_rate - left.metrics.defect_detection_rate,
        false_negative_delta=right.metrics.false_negatives - left.metrics.false_negatives,
        coverage_delta=right.metrics.total - left.metrics.total,
        duration_delta_ms=right.metrics.mean_duration_ms - left.metrics.mean_duration_ms,
        agent_steps=right.agent_steps,
        agent_tokens=right.agent_tokens,
    )


def render_comparison(comparison: Comparison, suite_name: str) -> str:
    left = comparison.left
    right = comparison.right
    lines = [
        f"# 规划器对比：{suite_name}",
        "",
        f"- 左：{comparison.left_label}",
        f"- 右：{comparison.right_label}",
        "",
        "| 指标 | 左 | 右 | 差 |",
        "| --- | --- | --- | --- |",
        f"| 覆盖的用例 | {left.total} | {right.total} | {comparison.coverage_delta:+d} |",
        f"| 与标注一致 | {left.matched} | {right.matched} | {right.matched - left.matched:+d} |",
        f"| 准确率（分母是各自覆盖数） | {left.accuracy:.1%} | {right.accuracy:.1%} | — |",
        f"| 发现违约 / 注入违约 | {left.detected_defects}/{left.total_defects} | {right.detected_defects}/{right.total_defects} | — |",
        f"| **违约发现率** | **{left.defect_detection_rate:.1%}** | **{right.defect_detection_rate:.1%}** | **{comparison.detection_delta:+.1%}** |",
        f"| 假阴性（漏报） | {left.false_negatives} | {right.false_negatives} | {comparison.false_negative_delta:+d} |",
        f"| 假阳性（误报） | {left.false_positives} | {right.false_positives} | {right.false_positives - left.false_positives:+d} |",
        f"| 未被覆盖的标注 | {left.uncovered} | {right.uncovered} | {right.uncovered - left.uncovered:+d} |",
        f"| 平均单用例耗时 | {left.mean_duration_ms}ms | {right.mean_duration_ms}ms | {comparison.duration_delta_ms:+d}ms |",
        f"| Agent 步数 / token | — | {comparison.agent_steps} / {comparison.agent_tokens} | — |",
        "",
        "## 怎么读这张表",
        "",
        "- **只有违约发现率可以直接相减**：它的分母是样例集里全部注入的违约，两次回放共享同一个分母。",
        "- 准确率不可比：它的分母是各自的覆盖数，覆盖得少的那个反而更容易好看。",
        "- 假阴性比假阳性严重得多：漏掉一个真实违约，比多报一个假警报代价高。",
        "",
        "## 声明",
        "",
        "这组数字只描述「在这一个基准、这一套标注、这一个用例集上」的差值。",
        "它不构成「某模式更准确」的一般结论；换基准、换标注、换用例设计者都会改变它。",
        "",
    ]
    return "\n".join(lines)


def gate(report: EvaluationReport, min_accuracy: float | None = None) -> tuple[ExitCode, str]:
    """评估的 CI 门禁规则。

    漏报比误报危险得多：**假阴性单独构成失败，不参与准确率平均**。
    指定了门槛就按门槛判，否则任何不一致都算失败（更严格）。
    """
    metrics = report.metrics
    if metrics.false_negatives > 0:
        return (
            ExitCode.ASSERTION_FAILED,
            f"有 {metrics.false_negatives} 条违约被漏报（标注为失败、实际通过）",
        )
    if min_accuracy is not None:
        if metrics.accuracy < min_accuracy:
            return (
                ExitCode.ASSERTION_FAILED,
                f"判定准确率 {metrics.accuracy:.1%} 低于门槛 {min_accuracy:.1%}",
            )
        return ExitCode.OK, ""
    if metrics.matched != metrics.total:
        return ExitCode.ASSERTION_FAILED, f"{metrics.total - metrics.matched} 条与标注不一致"
    return ExitCode.OK, ""


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
        f"| 注入的违约 / 发现 | {metrics.total_defects} / {metrics.detected_defects} |",
        f"| **漏报率**（1 - 发现率） | **{1 - metrics.defect_detection_rate:.1%}** |",
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
