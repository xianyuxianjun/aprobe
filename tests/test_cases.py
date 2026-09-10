from __future__ import annotations

import pytest

from aprobe.cases import load_case_file, order_cases, validate_cases
from aprobe.errors import CaseFileError
from aprobe.generator import generate_cases
from aprobe.models import Assertion, AssertionKind, RequestSpec, TestCase


def make(case_id: str, operation_id: str = "listPets", **overrides) -> TestCase:
    payload = {
        "id": case_id,
        "operation_id": operation_id,
        "request": RequestSpec(method="GET", path="/pets"),
        "assertions": [Assertion(kind=AssertionKind.STATUS, **{"in": [200]})],
    }
    payload.update(overrides)
    return TestCase(**payload)


def test_committed_case_file_is_valid(specification, cases_path) -> None:
    assert validate_cases(load_case_file(cases_path), specification) == []


def test_duplicate_id_is_reported(specification) -> None:
    problems = validate_cases([make("list-pets"), make("list-pets")], specification)
    assert any("重复" in problem for problem in problems)


def test_unknown_operation_is_reported(specification) -> None:
    problems = validate_cases([make("x", operation_id="nope")], specification)
    assert any("不存在 operation_id" in problem for problem in problems)


def test_stale_path_and_method_are_reported(specification) -> None:
    stale = make("x", request=RequestSpec(method="GET", path="/old-pets"))
    problems = validate_cases([stale], specification)
    assert any("请求路径" in problem for problem in problems)
    wrong_method = make("y", request=RequestSpec(method="POST", path="/pets"))
    problems = validate_cases([wrong_method], specification)
    assert any("请求方法" in problem or "写方法" in problem for problem in problems)


def test_case_without_assertion_is_rejected() -> None:
    problems = validate_cases([make("x", assertions=[])])
    assert any("没有任何 Assertion" in problem for problem in problems)


def test_write_declaration_must_match_method() -> None:
    problems = validate_cases([make("x", write=True)])
    assert any("只读方法" in problem for problem in problems)
    post = make("y", request=RequestSpec(method="POST", path="/pets"))
    problems = validate_cases([post])
    assert any("写方法" in problem for problem in problems)


def test_requires_must_resolve_and_not_cycle() -> None:
    dangling = make("a", requires=["missing"])
    assert any("不存在的用例" in problem for problem in validate_cases([dangling]))
    self_ref = make("a", requires=["a"])
    assert any("指向自己" in problem for problem in validate_cases([self_ref]))
    cycle = [make("a", requires=["b"]), make("b", requires=["a"])]
    assert any("环" in problem for problem in validate_cases(cycle))


def test_id_pattern_is_enforced() -> None:
    problems = validate_cases([make("Bad ID")])
    assert any("只允许小写字母" in problem for problem in problems)


def test_order_cases_is_level_ordered_then_alphabetical() -> None:
    cases = [make("c", requires=["b"]), make("b", requires=["a"]), make("a"), make("d")]
    assert [item.id for item in order_cases(cases)] == ["a", "d", "b", "c"]


def test_order_cases_rejects_cycle() -> None:
    with pytest.raises(CaseFileError):
        order_cases([make("a", requires=["b"]), make("b", requires=["a"])])


def test_generate_skips_operations_needing_input(specification) -> None:
    result = generate_cases(specification)
    assert [item.id for item in result.cases] == ["get-health", "get-pet-stats", "list-pets"]
    reasons = {item.operation_id: item.reason for item in result.needs_input}
    assert set(reasons) == {"createPet", "getPetById", "getPetOwner"}
    assert "路径参数" in reasons["getPetById"]
    assert "请求体" in reasons["createPet"]


def test_generated_cases_have_status_and_schema_assertions(specification) -> None:
    result = generate_cases(specification)
    list_pets = next(item for item in result.cases if item.id == "list-pets")
    kinds = [assertion.kind for assertion in list_pets.assertions]
    assert AssertionKind.STATUS in kinds
    assert AssertionKind.JSON_SCHEMA in kinds
    assert validate_cases(result.cases, specification) == []


def test_generate_never_invents_a_path_value(specification) -> None:
    """确定性生成绝不能为缺少输入的 Operation 造出"随便试一下"的请求。"""
    result = generate_cases(specification)
    assert all(not case.request.path_values for case in result.cases)


def test_case_file_does_not_leak_json_pointers(cases_path) -> None:
    """JSON Pointer 是 specification 的实现细节，不允许出现在人工审阅的用例文件里。"""
    text = cases_path.read_text(encoding="utf-8")
    assert "~1" not in text
    assert "#/" not in text


def test_unknown_response_code_in_a_case_is_reported(specification) -> None:
    problems = validate_cases(
        [make("x", assertions=[Assertion(kind=AssertionKind.JSON_SCHEMA, response="418")])],
        specification,
    )
    assert any("未声明的响应码 418" in problem for problem in problems)


def test_deterministic_generation_is_reproducible(specification, cases_path) -> None:
    """已提交用例文件里 origin=deterministic 的部分，必须与重新生成的结果逐字段一致。

    这在规范或生成逻辑变动时会把"用例已陈旧"变成一条可运行的失败，
    而不是靠人眼发现。
    """
    generated = generate_cases(specification).cases
    committed = [case for case in load_case_file(cases_path) if case.origin == "deterministic"]
    assert committed == generated
