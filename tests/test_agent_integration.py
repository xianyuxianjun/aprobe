"""Agent 模式的端到端验证：真实的 HTTP 模型客户端 + 真实的有界循环 + 真实的用例文件。

这里刻意不用 ScriptedModelClient——那个 adapter 已经在 test_agent_loop 里测过了。
本文件的目的是证明 OpenAI-compatible 客户端、循环、CLI 与用例文件**真的接得上**，
而且 Agent 能覆盖确定性生成覆盖不了的东西（需要路径参数的 Operation）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import write_config
from model_server import ModelService

from aprobe.cases import load_case_file, validate_cases
from aprobe.cli import main
from aprobe.specification import load_specification
from aprobe.trace import TraceStore


pytest.importorskip("langgraph.graph", reason="这些用例需要 [agent] 可选依赖")

def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


@pytest.fixture
def model_service():
    """脚本：先查契约，再提交一条需要路径参数的用例，最后停。"""
    script = [
        {
            "content": "先看 getPetById 的契约。",
            "tool_calls": [
                tool_call("get_operation", {"operation_id": "getPetById"}, "call_1"),
                tool_call("get_response_schema", {"operation_id": "getPetById", "status": "200"}, "call_2"),
            ],
        },
        {
            "content": "提交一条用例。",
            "tool_calls": [
                tool_call(
                    "submit_case",
                    {
                        "id": "get-pet-by-id-agent",
                        "operation_id": "getPetById",
                        "summary": "Agent 补上的路径参数用例",
                        "request": {"method": "GET", "path": "/pets/{petId}", "path_values": {"petId": "1"}},
                        "assertions": [
                            {"kind": "status", "in": [200]},
                            {"kind": "json_schema", "response": "200"},
                        ],
                    },
                    "call_3",
                )
            ],
        },
        {"content": "已覆盖 getPetById；其余 Operation 缺少请求体取值。"},
    ]
    service = ModelService(script).start()
    yield service
    service.stop()


def prepare(tmp_path: Path, spec_path: Path, capsys) -> Path:
    """先用降级模式产出 3 条确定性用例，再让 Agent 在它们之上继续。"""
    cases = tmp_path / "cases.yaml"
    assert main(["generate", "--spec", str(spec_path), "--out", str(cases)]) == 0
    capsys.readouterr()
    return write_config(tmp_path, base_url="http://127.0.0.1:1", spec=spec_path, cases=cases)


def use_model(monkeypatch, service: ModelService) -> None:
    monkeypatch.setenv("APROBE_MODEL_BASE_URL", service.base_url)
    monkeypatch.setenv("APROBE_MODEL", "mock-model")
    monkeypatch.setenv("APROBE_MODEL_API_KEY", "test-key")


def test_agent_mode_covers_what_deterministic_generation_cannot(
    tmp_path, spec_path, model_service, monkeypatch, capsys
) -> None:
    config = prepare(tmp_path, spec_path, capsys)
    use_model(monkeypatch, model_service)

    code = main(["generate", "--config", str(config), "--mode", "agent", "--force"])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "规划器：agent（agent）" in output
    assert "用例：4 条（新增 1）" in output
    assert "getPetById" not in output.split("未被覆盖的")[-1]

    cases = load_case_file(config.parent / "cases.yaml")
    ids = [case.id for case in cases]
    assert ids.count("get-pet-by-id-agent") == 1
    agent_case = next(case for case in cases if case.id == "get-pet-by-id-agent")
    assert agent_case.origin == "agent"
    assert agent_case.request.path_values == {"petId": "1"}
    assert validate_cases(cases, load_specification(spec_path)) == []


def test_agent_run_is_recorded_as_authoritative_trace(tmp_path, spec_path, model_service, monkeypatch, capsys) -> None:
    config = prepare(tmp_path, spec_path, capsys)
    use_model(monkeypatch, model_service)
    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 0
    capsys.readouterr()

    store = TraceStore(config.parent / ".aprobe" / "trace.db")
    runs = [run for run in store.list_agent_runs() if run.mode == "agent"]
    assert len(runs) == 1
    run = runs[0]
    assert run.model == "mock-model"
    assert run.consumed_steps == 3
    assert run.consumed_tokens == 3 * 150
    assert run.termination_reason.value == "completed"
    assert run.produced_case_ids == ["get-pet-by-id-agent"]

    names = [call.name for step in run.steps for call in step.tool_calls]
    assert names == ["get_operation", "get_response_schema", "submit_case"]
    assert all(call.ok for step in run.steps for call in step.tool_calls)
    # 每一步的 token 用量都落库，否则"单用例成本"无法计算
    assert all(step.input_tokens == 120 and step.output_tokens == 30 for step in run.steps)


def test_model_endpoint_receives_tool_declarations_and_no_target(
    tmp_path, spec_path, model_service, monkeypatch, capsys
) -> None:
    config = prepare(tmp_path, spec_path, capsys)
    use_model(monkeypatch, model_service)
    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 0
    capsys.readouterr()

    assert model_service.requests
    first = model_service.requests[0]
    assert first["model"] == "mock-model"
    declared = {item["function"]["name"] for item in first["tools"]}
    assert declared == {
        "list_operations",
        "get_operation",
        "get_response_schema",
        "list_existing_cases",
        "get_case_history",
        "submit_case",
    }
    # 模型端点不该看到被测目标：这是 ADR-0001 的边界
    assert "127.0.0.1:1" not in json.dumps(first, ensure_ascii=False)


def test_model_endpoint_failure_keeps_the_case_file_reviewable(tmp_path, spec_path, monkeypatch, capsys) -> None:
    """模型端点不可用时，规划以 planner_failed 终止，退出码是 2（既非通过也非失败），
    已有的用例文件保持原样——不会因为模型挂了就丢掉已审阅的内容。"""
    config = prepare(tmp_path, spec_path, capsys)
    monkeypatch.setenv("APROBE_MODEL_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("APROBE_MODEL", "mock-model")
    before = (config.parent / "cases.yaml").read_text(encoding="utf-8")

    code = main(["generate", "--config", str(config), "--mode", "agent", "--force"])
    output = capsys.readouterr().out
    assert code == 2, output
    assert "planner_failed" in output
    assert (config.parent / "cases.yaml").read_text(encoding="utf-8") == before


def test_generate_records_only_agent_facts_not_test_runs(tmp_path, spec_path, model_service, monkeypatch, capsys) -> None:
    config = prepare(tmp_path, spec_path, capsys)
    use_model(monkeypatch, model_service)
    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 0
    capsys.readouterr()

    store = TraceStore(config.parent / ".aprobe" / "trace.db")
    assert store.count_agent_runs() >= 1
    assert store.count() == 0, "generate 不该写入 TestRun：那是 run 的事实"
    payload = json.loads(store.list_agent_runs()[-1].model_dump_json())
    assert payload["mode"] == "agent"
    assert yaml.safe_load((config.parent / "cases.yaml").read_text(encoding="utf-8"))["cases"]
