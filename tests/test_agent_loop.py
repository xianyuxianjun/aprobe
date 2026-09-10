from __future__ import annotations

from aprobe.agent_loop import PROMPT_VERSION, SYSTEM_PROMPT, AgentLoop
from aprobe.model_client import ModelError, ModelReply, ModelToolCall, ScriptedModelClient
from aprobe.models import AgentBudget, TerminationReason
from aprobe.tools import PlanContext, ToolRegistry


def reply(*calls: tuple[str, dict], text: str = "", input_tokens: int = 100, output_tokens: int = 20) -> ModelReply:
    return ModelReply(
        text=text,
        tool_calls=[
            ModelToolCall(id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason="tool_calls" if calls else "stop",
    )


def valid_submit(case_id: str = "list-pets-by-agent") -> tuple[str, dict]:
    return (
        "submit_case",
        {
            "id": case_id,
            "operation_id": "listPets",
            "summary": "Agent 生成的列表契约校验",
            "request": {"method": "GET", "path": "/pets"},
            "assertions": [{"kind": "status", "in": [200]}],
        },
    )


def make_loop(specification, replies, budget: AgentBudget | None = None):
    registry = ToolRegistry(PlanContext(specification))
    client = ScriptedModelClient(replies)
    return AgentLoop(registry, client, budget or AgentBudget()), registry, client


def test_happy_path_produces_a_case_and_a_full_trace(specification) -> None:
    loop, registry, client = make_loop(
        specification,
        [
            reply(("list_operations", {}), ("get_response_schema", {"operation_id": "listPets", "status": "200"})),
            reply(valid_submit()),
            reply(text="已覆盖 listPets；其余 Operation 需要路径参数或请求体。"),
        ],
    )
    outcome = loop.run()
    run = outcome.agent_run

    assert [case.id for case in registry.context.submitted] == ["list-pets-by-agent"]
    assert run.termination_reason is TerminationReason.COMPLETED
    assert run.mode == "agent"
    assert run.consumed_steps == 3
    assert run.consumed_tokens == 3 * 120
    assert run.produced_case_ids == ["list-pets-by-agent"]
    assert run.prompt_version == PROMPT_VERSION
    assert run.model == "scripted"

    first_step = run.steps[0]
    assert [call.name for call in first_step.tool_calls] == ["list_operations", "get_response_schema"]
    assert all(call.ok for call in first_step.tool_calls)
    assert all(call.duration_ms >= 0 for call in first_step.tool_calls)
    assert first_step.tool_calls[0].result_summary == "返回 6 个 Operation"
    assert first_step.input_tokens == 100 and first_step.output_tokens == 20


def test_every_step_sees_the_system_prompt_and_the_tool_declarations(specification) -> None:
    loop, _, client = make_loop(specification, [reply(), reply()])
    loop.run()
    assert client.calls
    for call in client.calls:
        assert call["system"] == SYSTEM_PROMPT
        assert {item["function"]["name"] for item in call["tools"]} >= {"submit_case", "get_response_schema"}


def test_no_tool_call_ends_the_loop_immediately(specification) -> None:
    """模型第一句就选择停下时，循环不能空转到预算耗尽。"""
    loop, _, _ = make_loop(specification, [reply(text="没什么可测的")])
    run = loop.run().agent_run
    assert run.consumed_steps == 1
    assert run.termination_reason is TerminationReason.COMPLETED
    assert run.produced_case_ids == []


def test_budget_exhaustion_is_neither_pass_nor_fail(specification) -> None:
    looping = [reply(("list_operations", {})) for _ in range(5)]
    loop, _, _ = make_loop(specification, looping, AgentBudget(max_steps=2))
    run = loop.run().agent_run
    assert run.consumed_steps == 2
    assert run.termination_reason is TerminationReason.BUDGET_EXHAUSTED
    assert any("步数上限" in note for note in run.notes)


def test_token_budget_stops_the_loop(specification) -> None:
    looping = [reply(("list_operations", {}), input_tokens=600, output_tokens=0) for _ in range(5)]
    loop, _, _ = make_loop(specification, looping, AgentBudget(max_steps=10, max_tokens=1000))
    run = loop.run().agent_run
    assert run.consumed_steps == 2
    assert run.termination_reason is TerminationReason.BUDGET_EXHAUSTED
    assert any("token 上限" in note for note in run.notes)


def test_model_failure_is_recorded_as_planner_failed(specification) -> None:
    class BrokenClient:
        name = "broken"

        def complete(self, *, system, messages, tools):
            raise ModelError("模型端点调用失败: ConnectError")

    registry = ToolRegistry(PlanContext(specification))
    run = AgentLoop(registry, BrokenClient()).run().agent_run
    assert run.termination_reason is TerminationReason.PLANNER_FAILED
    assert run.produced_case_ids == []
    assert any("模型调用失败" in note for note in run.notes)
    assert run.steps and run.steps[0].duration_ms >= 0


def test_rejection_feedback_reaches_the_model(specification) -> None:
    loop, registry, client = make_loop(
        specification,
        [
            reply(("submit_case", {**valid_submit()[1], "operation_id": "nope"})),
            reply(valid_submit()),
            reply(text="done"),
        ],
    )
    run = loop.run().agent_run
    assert registry.context.submitted
    second_call_messages = client.calls[1]["messages"]
    tool_messages = [item for item in second_call_messages if item.get("role") == "tool"]
    assert tool_messages, "被拒绝的提交必须作为工具结果回传给模型"
    assert "不存在 operation_id" in tool_messages[0]["content"]
    assert run.notes, "被拒绝的提交要进轨迹"


def test_malformed_tool_arguments_do_not_crash_the_loop(specification) -> None:
    bad = ModelReply(
        tool_calls=[ModelToolCall(id="c1", name="get_operation", arguments={}, parse_error="工具参数不是合法 JSON")]
    )
    loop, _, _ = make_loop(specification, [bad, reply(text="ok")])
    run = loop.run().agent_run
    assert run.steps[0].tool_calls[0].ok is False
    assert run.steps[0].tool_calls[0].error


def test_loop_never_receives_the_target_address(specification) -> None:
    loop, _, client = make_loop(specification, [reply(("list_operations", {})), reply()])
    loop.run()
    transcript = str(client.calls)
    for leak in ("http://", "127.0.0.1", "Authorization"):
        assert leak not in transcript
