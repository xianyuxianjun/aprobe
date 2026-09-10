from __future__ import annotations

import pytest

from aprobe.tools import PlanContext, ToolRegistry, history_from_runs

#: 生成阶段允许存在的工具，一个不多一个不少。
#: 这个清单是 ADR-0001 的可执行形式：任何"能发请求"的工具都会让它失败。
ALLOWED_TOOLS = {
    "list_operations",
    "get_operation",
    "get_response_schema",
    "list_existing_cases",
    "get_case_history",
    "submit_case",
}


@pytest.fixture
def registry(specification) -> ToolRegistry:
    return ToolRegistry(PlanContext(specification))


def valid_case_payload(**overrides) -> dict:
    payload = {
        "id": "list-pets-by-agent",
        "operation_id": "listPets",
        "summary": "Agent 生成的列表契约校验",
        "request": {"method": "GET", "path": "/pets"},
        "assertions": [{"kind": "status", "in": [200]}],
    }
    payload.update(overrides)
    return payload


def test_registry_exposes_exactly_the_read_only_tool_set(registry) -> None:
    assert set(registry.names) == ALLOWED_TOOLS
    declarations = registry.tool_declarations()
    assert {item["function"]["name"] for item in declarations} == ALLOWED_TOOLS
    for item in declarations:
        assert item["type"] == "function"
        assert item["function"]["parameters"]["type"] == "object"


def test_generation_context_has_no_target(registry) -> None:
    """Agent 拿不到被测目标地址——这是 ADR-0001 的边界，测试要守住它。"""
    context = registry.context
    for attribute in ("target", "base_url", "url"):
        assert not hasattr(context, attribute)


def test_unknown_tool_is_rejected(registry) -> None:
    result = registry.call("send_request", {})
    assert result.ok is False
    assert result.error == "unknown_tool"


def test_invalid_arguments_are_rejected_by_the_declared_schema(registry) -> None:
    missing = registry.call("get_operation", {})
    assert missing.ok is False
    assert "operation_id" in missing.error
    extra = registry.call("list_operations", {"unexpected": 1})
    assert extra.ok is False


def test_unknown_operation_is_reported_not_raised(registry) -> None:
    result = registry.call("get_operation", {"operation_id": "nope"})
    assert result.ok is False
    assert "不存在 operation_id" in result.error


def test_list_operations_reports_coverage(registry) -> None:
    result = registry.call("list_operations", {})
    assert result.ok
    assert result.payload["total"] == 6
    by_id = {item["operation_id"]: item for item in result.payload["operations"]}
    assert by_id["getPetById"]["has_required_input"] is True
    assert by_id["listPets"]["already_covered"] is False
    assert by_id["listPets"]["declared_responses"] == ["200"]


def test_get_response_schema_does_not_expose_pointers(registry) -> None:
    result = registry.call("get_response_schema", {"operation_id": "listPets", "status": "200"})
    assert result.ok
    assert result.payload["schema"]["required"] == ["items", "total"]
    assert "~1" not in str(result.payload)

    bad = registry.call("get_response_schema", {"operation_id": "listPets", "status": "418"})
    assert bad.ok is False


def test_submit_case_accepts_a_valid_case(registry) -> None:
    result = registry.call("submit_case", valid_case_payload())
    assert result.ok
    assert result.payload["accepted"] is True
    assert [case.id for case in registry.context.submitted] == ["list-pets-by-agent"]


def test_submit_case_rejects_unknown_operation(registry) -> None:
    result = registry.call("submit_case", valid_case_payload(operation_id="nope"))
    assert result.ok is False
    assert any("不存在 operation_id" in problem for problem in result.payload["problems"])


def test_submit_case_rejects_path_that_contradicts_the_spec(registry) -> None:
    payload = valid_case_payload(request={"method": "GET", "path": "/pets/"})
    result = registry.call("submit_case", payload)
    assert result.ok is False
    assert any("请求路径" in problem for problem in result.payload["problems"])


def test_submit_case_rejects_empty_assertions(registry) -> None:
    result = registry.call("submit_case", valid_case_payload(assertions=[]))
    assert result.ok is False


def test_submit_case_rejects_undeclared_response_code(registry) -> None:
    payload = valid_case_payload(assertions=[{"kind": "json_schema", "response": "418"}])
    result = registry.call("submit_case", payload)
    assert result.ok is False
    assert any("未声明的响应码" in problem for problem in result.payload["problems"])


def test_submit_case_rejects_write_method_without_declaration(registry) -> None:
    payload = valid_case_payload(
        operation_id="createPet",
        request={"method": "POST", "path": "/pets", "body": {"name": "Ada"}},
    )
    result = registry.call("submit_case", payload)
    assert result.ok is False
    assert any("write" in problem for problem in result.payload["problems"])


def test_submit_case_rejects_duplicate_id(registry) -> None:
    assert registry.call("submit_case", valid_case_payload()).ok
    again = registry.call("submit_case", valid_case_payload())
    assert again.ok is False
    assert any("已存在" in problem for problem in again.payload["problems"])


def test_submit_case_rejects_id_colliding_with_an_existing_case(specification) -> None:
    from aprobe.models import Assertion, AssertionKind, RequestSpec, TestCase

    existing = TestCase(
        id="list-pets",
        operation_id="listPets",
        request=RequestSpec(method="GET", path="/pets"),
        assertions=[Assertion(kind=AssertionKind.STATUS, **{"in": [200]})],
        origin="human",
    )
    registry = ToolRegistry(PlanContext(specification, existing_cases=[existing]))
    result = registry.call("submit_case", valid_case_payload(id="list-pets"))
    assert result.ok is False
    assert any("已存在" in problem for problem in result.payload["problems"])


def test_rejected_cases_are_recorded_for_the_trace(registry) -> None:
    registry.call("submit_case", valid_case_payload(operation_id="nope"))
    assert registry.context.rejected


def test_history_is_derived_from_test_runs() -> None:
    from datetime import datetime, timezone

    from aprobe.models import TerminationReason, TestRun, Verdict

    run = TestRun(
        run_id="r1",
        case_id="list-pets",
        operation_id="listPets",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_ms=5,
        verdict=Verdict.FAILED,
        termination_reason=TerminationReason.COMPLETED,
    )
    history = history_from_runs([run])
    assert history["listPets"][0]["verdict"] == "failed"
