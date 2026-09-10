from __future__ import annotations

import importlib.util

import pytest

from aprobe.errors import ConfigError
from aprobe.models import (
    AGENT_PLANNER_VERSION,
    DETERMINISTIC_PLANNER_VERSION,
    AgentBudget,
    Assertion,
    AssertionKind,
    RequestSpec,
    TestCase,
)
from aprobe.model_client import ModelReply, ModelToolCall, ScriptedModelClient
from aprobe.planner import plan, planner_label

def _has_langgraph() -> bool:
    # find_spec 对带点的名字会先导入父包，所以缺依赖时会抛而不是返回 None
    try:
        return importlib.util.find_spec("langgraph.graph") is not None
    except ModuleNotFoundError:
        return False


requires_agent = pytest.mark.skipif(not _has_langgraph(), reason="需要 [agent] 可选依赖")


def submit(case_id: str, operation_id: str, path: str) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ModelToolCall(
                id="c1",
                name="submit_case",
                arguments={
                    "id": case_id,
                    "operation_id": operation_id,
                    "summary": f"{operation_id} 的契约校验",
                    "request": {"method": "GET", "path": path},
                    "assertions": [{"kind": "status", "in": [200]}],
                },
            )
        ]
    )


def human_case(case_id: str = "list-pets") -> TestCase:
    return TestCase(
        id=case_id,
        operation_id="listPets",
        request=RequestSpec(method="GET", path="/pets"),
        assertions=[Assertion(kind=AssertionKind.STATUS, **{"in": [200]})],
        origin="human",
    )


def test_degraded_mode_needs_no_model(specification) -> None:
    result = plan(specification, mode="degraded")
    assert [case.id for case in result.cases] == ["get-health", "get-pet-stats", "list-pets"]
    assert result.agent_run is not None
    assert result.agent_run.mode == "degraded"
    assert result.agent_run.consumed_steps == 0
    assert result.agent_run.produced_case_ids == ["get-health", "get-pet-stats", "list-pets"]
    assert {item.operation_id for item in result.needs_input} == {"createPet", "getPetById", "getPetOwner"}


def test_auto_falls_back_to_degraded_without_a_model(specification) -> None:
    result = plan(specification, mode="auto")
    assert result.agent_run is not None
    assert result.agent_run.mode == "degraded"
    assert any("退回降级模式" in note for note in result.agent_run.notes)


def test_agent_mode_without_a_model_is_a_configuration_error(specification) -> None:
    with pytest.raises(ConfigError, match="agent 模式需要模型端点"):
        plan(specification, mode="agent")


def test_unknown_mode_is_rejected(specification) -> None:
    with pytest.raises(ConfigError, match="未知的规划模式"):
        plan(specification, mode="telepathy")


@requires_agent
def test_agent_mode_produces_the_same_artifact_shape(specification) -> None:
    client = ScriptedModelClient([submit("list-pets-by-agent", "listPets", "/pets"), ModelReply(text="done")])
    result = plan(specification, mode="agent", model=client)

    assert [case.id for case in result.cases] == ["list-pets-by-agent"]
    assert result.cases[0].origin == "agent"
    assert result.agent_run is not None and result.agent_run.mode == "agent"
    # 未覆盖的 Operation 由确定性代码算出，不是模型自述
    assert {item.operation_id for item in result.needs_input} == {
        "createPet",
        "getHealth",
        "getPetById",
        "getPetOwner",
        "getPetStats",
    }
    assert all(item.reason for item in result.needs_input)


def test_regenerating_over_an_existing_file_keeps_the_reviewed_versions(specification) -> None:
    """已有用例优先：它们已经被人审阅过，不能被重新生成的结果静默替换。"""
    reviewed = plan(specification, mode="degraded").cases
    again = plan(specification, mode="degraded", existing_cases=reviewed)
    assert [case.id for case in again.cases] == [case.id for case in reviewed]
    assert any("保留已有版本" in note for note in again.agent_run.notes)


def test_human_cases_are_kept_alongside_generated_ones(specification) -> None:
    reviewed = human_case("list-pets")  # 已审阅的人工版本，与确定性生成的 list-pets 同 id
    result = plan(specification, mode="degraded", existing_cases=[reviewed])
    ids = [case.id for case in result.cases]
    assert ids[0] == "list-pets"
    assert result.cases[0].origin == "human", "人工版本必须胜出"
    assert ids.count("list-pets") == 1
    assert {"get-health", "get-pet-stats"} <= set(ids)
    assert any("保留已有版本" in note for note in result.agent_run.notes)


@requires_agent
def test_agent_mode_passes_budget_into_the_trace(specification) -> None:
    client = ScriptedModelClient([ModelReply(text="nothing to do")])
    result = plan(specification, mode="agent", model=client, budget=AgentBudget(max_steps=3, max_tokens=99))
    assert result.agent_run.budget.max_steps == 3
    assert result.agent_run.budget.max_tokens == 99


def test_planner_label_identifies_the_producing_mode() -> None:
    assert planner_label([]) == "none"
    agent_case = human_case().model_copy(update={"origin": "agent"})
    assert planner_label([agent_case]) == AGENT_PLANNER_VERSION
    assert planner_label([human_case()]) == "human"
    deterministic = human_case().model_copy(update={"origin": "deterministic"})
    assert planner_label([deterministic]) == DETERMINISTIC_PLANNER_VERSION
    assert planner_label([human_case(), agent_case]) == "mixed"
