"""失败归因（M3）与它进入报告的路径（M4）。

关键边界都在这里被守住：
- 诊断工具集里**没有**能改用例文件的工具；
- 没有证据的归因会被确定性代码拒绝（不靠提示词）；
- 归因不改变任何 Verdict，报告里必须与结论分开陈述。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import write_config
from model_server import ModelService

from aprobe.cli import main
from aprobe.diagnosis import DIAGNOSE_PROMPT_VERSION, DiagnosisContext, diagnose
from aprobe.errors import ConfigError
from aprobe.model_client import ModelReply, ModelToolCall, ScriptedModelClient
from aprobe.models import (
    Assertion,
    AssertionKind,
    FailureCategory,
    Observation,
)
from aprobe.tools import ToolRegistry

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ALLOWED_DIAGNOSIS_TOOLS = {
    "get_operation",
    "get_response_schema",
    "get_run",
    "get_case",
    "list_related_runs",
    "submit_diagnosis",
}


pytest.importorskip("langgraph.graph", reason="这些用例需要 [agent] 可选依赖")

def failing_run() -> "TestRun":  # noqa: F821
    from aprobe.models import AssertionResult, TerminationReason, TestRun, Verdict, RunProvenance

    result = AssertionResult(
        assertion=Assertion(kind=AssertionKind.JSON_SCHEMA, response="200"),
        verdict=Verdict.FAILED,
        observed="契约校验失败于 total",
        detail="'2' is not of type 'integer'",
    )
    return TestRun(
        run_id="run-failed",
        case_id="get-pet-stats",
        operation_id="getPetStats",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_ms=7,
        verdict=Verdict.FAILED,
        termination_reason=TerminationReason.COMPLETED,
        request={"method": "GET", "path": "/pets/stats", "url": "http://127.0.0.1:1/pets/stats"},
        observation=Observation(status_code=200, body_json={"total": "2"}, body_text='{"total": "2"}'),
        assertion_results=[result],
        provenance=RunProvenance(target="http://127.0.0.1:1"),
    )


def passing_run():
    run = failing_run()
    return run.model_copy(update={"run_id": "run-passed", "verdict": run.verdict.PASSED})


def context(specification, run=None) -> DiagnosisContext:
    return DiagnosisContext(specification, run or failing_run())


def test_diagnosis_has_no_tool_that_can_change_the_case_file(specification) -> None:
    ctx = context(specification)
    from aprobe.diagnosis import _build_specs

    registry = ToolRegistry(ctx, specs=_build_specs(ctx))
    assert set(registry.names) == ALLOWED_DIAGNOSIS_TOOLS
    assert "submit_case" not in registry.names


def test_get_run_exposes_the_execution_facts(specification) -> None:
    payload = context(specification).get_run()
    assert payload["verdict"] == "failed"
    assert payload["status_code"] == 200
    assert payload["request"]["path"] == "/pets/stats"
    assert payload["assertions"][0]["detail"] == "'2' is not of type 'integer'"


def test_get_case_reports_a_missing_case(specification) -> None:
    payload = context(specification).get_case()
    assert "error" in payload
    assert "不在当前用例文件中" in payload["error"]


def test_get_case_returns_the_case_definition(specification) -> None:
    from aprobe.cases import load_case_file

    cases = {case.id: case for case in load_case_file(Path("cases/petstore.yaml"))}
    ctx = DiagnosisContext(specification, failing_run(), case=cases["get-pet-stats"])
    payload = ctx.get_case()
    assert payload["operation_id"] == "getPetStats"
    assert payload["assertions"]


def test_submit_diagnosis_requires_evidence(specification) -> None:
    ctx = context(specification)
    result = ctx.submit_diagnosis({"category": "interface_defect", "reason": "看起来不对"})
    assert result["accepted"] is False
    assert any("evidence 不能为空" in problem for problem in result["problems"])


def test_submit_diagnosis_requires_a_reason(specification) -> None:
    ctx = context(specification)
    result = ctx.submit_diagnosis(
        {"category": "case_defect", "reason": "   ", "evidence": ["断言引用了未声明的响应码"]}
    )
    assert result["accepted"] is False
    assert any("reason 不能为空" in problem for problem in result["problems"])


def test_submit_diagnosis_rejects_an_unknown_category(specification) -> None:
    ctx = context(specification)
    result = ctx.submit_diagnosis({"category": "maybe_bug", "reason": "x", "evidence": ["y"]})
    assert result["accepted"] is False
    assert "未知类别" in result["problems"][0]


def test_submit_diagnosis_accepts_a_supported_attribution(specification) -> None:
    ctx = context(specification)
    result = ctx.submit_diagnosis(
        {
            "category": "interface_defect",
            "reason": "契约声明 total 是整数，实现返回了字符串",
            "evidence": ["响应体 total 的值为字符串 '2'", "json_schema 断言在 total 上失败"],
            "suggested_fix": "确认契约与实现哪一侧要改",
        }
    )
    assert result["accepted"] is True
    attribution = ctx.attribution
    assert attribution is not None
    assert attribution.category is FailureCategory.INTERFACE_DEFECT
    assert len(attribution.evidence) == 2


def test_diagnose_refuses_a_passing_run(specification) -> None:
    with pytest.raises(ConfigError, match="不需要归因"):
        diagnose(specification, passing_run(), model=ScriptedModelClient([]))


def test_diagnose_requires_a_model(specification) -> None:
    with pytest.raises(ConfigError, match="诊断需要模型端点"):
        diagnose(specification, failing_run(), model=None)


def attribution_reply() -> ModelReply:
    return ModelReply(
        tool_calls=[
            ModelToolCall(
                id="c1",
                name="submit_diagnosis",
                arguments={
                    "category": "interface_defect",
                    "reason": "契约声明 total 是整数，实现返回了字符串",
                    "evidence": ["响应体 total 是字符串"],
                },
            )
        ],
        input_tokens=50,
        output_tokens=10,
    )


def test_diagnose_records_a_full_trace(specification) -> None:
    client = ScriptedModelClient([attribution_reply(), ModelReply(text="done")])
    agent_run, attribution = diagnose(specification, failing_run(), model=client)

    assert attribution is not None
    assert attribution.agent_run_id == agent_run.run_id
    assert attribution.prompt_version == DIAGNOSE_PROMPT_VERSION
    assert attribution.model == "scripted"
    assert attribution.run_id == "run-failed"
    assert agent_run.mode == "diagnose"
    assert agent_run.consumed_steps == 2
    assert agent_run.consumed_tokens == 60
    # 归因不改变结论：原始 TestRun 的 Verdict 仍然是失败
    assert failing_run().verdict.value == "failed"


def test_diagnose_reports_when_the_model_gives_no_attribution(specification) -> None:
    client = ScriptedModelClient([ModelReply(text="我不想说")])
    agent_run, attribution = diagnose(specification, failing_run(), model=client)
    assert attribution is None
    assert any("没有提交归因" in note for note in agent_run.notes)


def test_rejected_attribution_is_fed_back_to_the_model(specification) -> None:
    bad = ModelReply(
        tool_calls=[
            ModelToolCall(
                id="c1",
                name="submit_diagnosis",
                arguments={"category": "interface_defect", "reason": "感觉不对"},
            )
        ]
    )
    client = ScriptedModelClient([bad, attribution_reply(), ModelReply(text="done")])
    agent_run, attribution = diagnose(specification, failing_run(), model=client)
    assert attribution is not None
    # 拒绝发生在工具声明的 schema 上，消息比“失败了”具体得多
    tool_messages = [item for item in client.calls[1]["messages"] if item.get("role") == "tool"]
    assert "evidence" in tool_messages[0]["content"]
    assert agent_run.steps[0].tool_calls[0].ok is False
    assert "evidence" in agent_run.steps[0].tool_calls[0].error or "evidence" in agent_run.steps[0].tool_calls[0].result_summary


# ---- CLI 端到端：违约基准 → 失败运行 → 归因 → 进报告 ----


def tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


@pytest.fixture
def diagnosis_model_service():
    script = [
        {
            "content": "先看事实与契约。",
            "tool_calls": [
                tool_call("get_run", {}, "c1"),
                tool_call("get_operation", {"operation_id": "getPetStats"}, "c2"),
            ],
        },
        {
            "content": "提交归因。",
            "tool_calls": [
                tool_call(
                    "submit_diagnosis",
                    {
                        "category": "interface_defect",
                        "reason": "契约声明 total 为整数，实现返回字符串",
                        "evidence": ['响应体 total 的值为字符串 "2"', "json_schema 断言在 total 处失败"],
                        "suggested_fix": "确认契约与实现哪一侧要改",
                    },
                    "c3",
                )
            ],
        },
        {"content": "归因完成。"},
    ]
    service = ModelService(script).start()
    yield service
    service.stop()


def test_cli_diagnose_end_to_end(tmp_path, spec_path, cases_path, violating, diagnosis_model_service, monkeypatch, capsys):
    config = write_config(tmp_path, base_url=violating.base_url, spec=spec_path, cases=cases_path)
    assert main(["run", "--config", str(config)]) == 1  # violating 基准上有一条失败
    capsys.readouterr()

    monkeypatch.setenv("APROBE_MODEL_BASE_URL", diagnosis_model_service.base_url)
    monkeypatch.setenv("APROBE_MODEL", "mock-model")
    code = main(["diagnose", "--config", str(config)])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "get-pet-stats: interface_defect" in output
    assert "归因 1/1 条，0 条没有依据" in output

    from aprobe.trace import TraceStore

    store = TraceStore(config.parent / ".aprobe" / "trace.db")
    attributions = store.list_attributions()
    assert len(attributions) == 1
    assert attributions[0].category is FailureCategory.INTERFACE_DEFECT
    assert attributions[0].evidence


def test_report_separates_attributions_from_verdicts(
    tmp_path, spec_path, cases_path, violating, diagnosis_model_service, monkeypatch, capsys
) -> None:
    config = write_config(tmp_path, base_url=violating.base_url, spec=spec_path, cases=cases_path)
    assert main(["run", "--config", str(config)]) == 1
    monkeypatch.setenv("APROBE_MODEL_BASE_URL", diagnosis_model_service.base_url)
    monkeypatch.setenv("APROBE_MODEL", "mock-model")
    assert main(["diagnose", "--config", str(config)]) == 0
    capsys.readouterr()

    assert main(["report", "--config", str(config), "--format", "md"]) == 0
    markdown = capsys.readouterr().out
    assert "## 失败归因（模型建议，不改变任何结论）" in markdown
    assert "interface_defect" in markdown
    assert "归因是建议，不是结论" in markdown

    assert main(["report", "--config", str(config), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["attributions"][0]["category"] == "interface_defect"
    # 归因在场，但没有改变任何一条运行结论
    assert payload["summary"]["failed"] == 1


def test_diagnose_reports_when_no_model_is_configured(tmp_path, spec_path, cases_path, capsys) -> None:
    config = write_config(tmp_path, base_url="http://127.0.0.1:1", spec=spec_path, cases=cases_path)
    assert main(["diagnose", "--config", str(config)]) == 3
    assert "诊断需要模型端点" in capsys.readouterr().err
