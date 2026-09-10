"""报告导出。

边界：报告只陈述 Trace 里已记录的事实；没有依据的推断不写进去，
并且必须显式声明"结果不等于完整保证"，同时列出未执行、失败与无法验证的测试。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import ExitCode
from .models import FailureAttribution, RunProvenance, TerminationReason, TestRun, Verdict


@dataclass
class ReportMeta:
    """报告的元信息。它的唯一来源是 Trace（ADR-0002），因此不可能与 Trace 不一致。"""

    provenance: RunProvenance = field(default_factory=RunProvenance)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_runs(cls, runs: list[TestRun]) -> "ReportMeta":
        return cls(provenance=runs[0].provenance if runs else RunProvenance())


def summarize(runs: list[TestRun]) -> dict[str, int]:
    summary = {"total": len(runs), "passed": 0, "failed": 0, "inconclusive": 0, "policy_denied": 0, "request_failed": 0}
    for run in runs:
        summary[run.verdict.value] += 1
        if run.termination_reason is TerminationReason.POLICY_DENIED:
            summary["policy_denied"] += 1
        if run.termination_reason is TerminationReason.REQUEST_FAILED:
            summary["request_failed"] += 1
    return summary


def _meta_block(meta: ReportMeta) -> list[str]:
    provenance = meta.provenance
    return [
        f"- 规范：`{provenance.spec_source}`（{provenance.spec_title} {provenance.spec_version}）",
        f"- 用例文件：`{provenance.cases_file}`",
        f"- 被测目标：`{provenance.target}`",
        f"- aprobe：{provenance.aprobe_version}　规划器：{provenance.planner}",
        f"- 生成时间：{meta.generated_at.isoformat(timespec='seconds')}",
    ]


def render_markdown(runs: list[TestRun], meta: ReportMeta, attributions: list[FailureAttribution] | None = None) -> str:
    summary = summarize(runs)
    lines: list[str] = ["# aprobe 接口契约测试报告", "", "## 运行范围", "", *_meta_block(meta), ""]
    lines += [
        "## 结果汇总",
        "",
        "| 结论 | 数量 |",
        "| --- | --- |",
        f"| 通过 | {summary['passed']} |",
        f"| 失败 | {summary['failed']} |",
        f"| 无法判定 | {summary['inconclusive']} |",
        f"| 合计 | {summary['total']} |",
        "",
        f"其中因策略被拒绝 {summary['policy_denied']} 条，因请求未完成 {summary['request_failed']} 条。",
        "",
        "## 逐条结果",
        "",
    ]
    for run in runs:
        lines += [
            f"### `{run.case_id}` → {run.verdict.value}",
            "",
            f"- Operation：`{run.operation_id}`",
            f"- 终止原因：`{run.termination_reason.value}`",
            f"- 请求：`{run.request.get('method', '')} {run.request.get('url', '')}`",
            f"- Trace：`run_id={run.run_id}`　耗时 {run.duration_ms}ms",
        ]
        if run.observation.error:
            lines.append(f"- 未收到响应：{run.observation.error}")
        if run.observation.status_code is not None:
            lines.append(f"- 状态码：{run.observation.status_code}")
        if run.assertion_results:
            lines += ["", "| Assertion | 结论 | 观察 | 说明 |", "| --- | --- | --- | --- |"]
            for result in run.assertion_results:
                detail = result.detail.replace("|", "\\|")
                observed = result.observed.replace("|", "\\|")
                lines.append(f"| `{result.assertion.kind.value}` | {result.verdict.value} | {observed} | {detail} |")
        lines.append("")

    not_passed = [run for run in runs if run.verdict is not Verdict.PASSED]
    lines += ["## 未通过、无法判定与未执行的测试", ""]
    if not_passed:
        for run in not_passed:
            lines.append(f"- `{run.case_id}`：{run.verdict.value}（{run.termination_reason.value}）")
    else:
        lines.append("- 无")
    lines += ["## 失败归因（模型建议，不改变任何结论）", ""]
    if attributions:
        for attribution in attributions:
            lines += [
                f"### `{attribution.case_id}` → {attribution.category.value}",
                "",
                f"- 理由：{attribution.reason}",
                f"- 来自 run_id={attribution.run_id}，模型 {attribution.model or '未记录'}，"
                f"提示版本 {attribution.prompt_version or '未记录'}",
            ]
            if attribution.suggested_fix:
                lines.append(f"- 建议方向：{attribution.suggested_fix}")
            if attribution.evidence:
                lines.append("- 证据：")
                lines += [f"  - {item}" for item in attribution.evidence]
            lines.append("")
    else:
        lines += ["- 无（未运行诊断，或诊断没有给出带证据的归因）", ""]
    lines += [
        "",
        "## 声明",
        "",
        "本报告只覆盖上述用例文件中已确认的测试范围；结果不等于完整保证，",
        "未验证的部分不等于不存在问题。所有结论均来自确定性 Assertion 求值，",
        "模型只参与用例设计与失败归因，不参与判定；归因是建议，不是结论。",
        "",
    ]
    return "\n".join(lines)


def _runs_payload(
    runs: list[TestRun], meta: ReportMeta, attributions: list[FailureAttribution] | None = None
) -> dict[str, object]:
    provenance = meta.provenance
    return {
        "meta": {
            "spec_source": provenance.spec_source,
            "spec_title": provenance.spec_title,
            "spec_version": provenance.spec_version,
            "target": provenance.target,
            "cases_file": provenance.cases_file,
            "aprobe_version": provenance.aprobe_version,
            "planner": provenance.planner,
            "generated_at": meta.generated_at.isoformat(),
        },
        "summary": summarize(runs),
        "runs": [json.loads(run.model_dump_json()) for run in runs],
        "attributions": [json.loads(item.model_dump_json()) for item in (attributions or [])],
    }


def render_json(
    runs: list[TestRun], meta: ReportMeta, attributions: list[FailureAttribution] | None = None
) -> str:
    return json.dumps(_runs_payload(runs, meta, attributions), ensure_ascii=False, indent=2)


def render_junit(
    runs: list[TestRun], meta: ReportMeta, attributions: list[FailureAttribution] | None = None
) -> str:
    summary = summarize(runs)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "aprobe",
            "tests": str(summary["total"]),
            "failures": str(summary["failed"]),
            "errors": str(summary["request_failed"] + summary["policy_denied"]),
            "skipped": str(summary["inconclusive"]),
            "timestamp": meta.generated_at.isoformat(timespec="seconds"),
            "hostname": meta.provenance.target or "unknown",
        },
    )
    properties = ElementTree.SubElement(suite, "properties")
    for name, value in (
        ("spec_source", meta.provenance.spec_source),
        ("spec_version", meta.provenance.spec_version),
        ("cases_file", meta.provenance.cases_file),
        ("aprobe_version", meta.provenance.aprobe_version),
        ("planner", meta.provenance.planner),
    ):
        ElementTree.SubElement(properties, "property", {"name": name, "value": value})

    for run in runs:
        testcase = ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": run.operation_id, "name": run.case_id, "time": f"{run.duration_ms / 1000:.3f}"},
        )
        detail = "; ".join(
            f"{result.assertion.kind.value}={result.verdict.value} ({result.observed})"
            for result in run.assertion_results
        ) or run.observation.error or run.termination_reason.value
        if run.verdict is Verdict.FAILED:
            failure = ElementTree.SubElement(testcase, "failure", {"message": run.termination_reason.value})
            failure.text = detail
        elif run.termination_reason in (TerminationReason.POLICY_DENIED, TerminationReason.REQUEST_FAILED):
            error = ElementTree.SubElement(testcase, "error", {"message": run.termination_reason.value})
            error.text = detail
        elif run.verdict is Verdict.INCONCLUSIVE:
            skipped = ElementTree.SubElement(testcase, "skipped", {"message": "无法判定"})
            skipped.text = detail
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True).decode("utf-8")


def gate_exit_code(runs: list[TestRun], fail_on: str = "failed") -> int:
    """把一批 TestRun 折成 CI 门禁退出码。

    优先序：目标被策略拒绝 > 断言失败 > 存在无法判定。
    fail_on="none" 只改退出码，不改任何已记录的结论。
    """
    if any(run.termination_reason is TerminationReason.POLICY_DENIED for run in runs):
        return ExitCode.POLICY_DENIED
    if fail_on == "none":
        return ExitCode.OK
    if any(run.verdict is Verdict.FAILED for run in runs):
        return ExitCode.ASSERTION_FAILED
    if any(run.verdict is Verdict.INCONCLUSIVE for run in runs):
        return ExitCode.ASSERTION_FAILED if fail_on == "inconclusive" else ExitCode.INCONCLUSIVE
    return ExitCode.OK


RENDERERS = {"md": render_markdown, "json": render_json, "junit": render_junit}
