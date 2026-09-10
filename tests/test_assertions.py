from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aprobe.assertions import AssertionEvaluator, decide_verdict, get_json_path
from aprobe.models import Assertion, AssertionKind, Observation, Verdict

OPERATION = "listPets"


def observation(**overrides) -> Observation:
    base = {
        "status_code": 200,
        "headers": {"Content-Type": "application/json"},
        "body_text": '{"total": 2, "items": [{"id": 1}]}',
        "body_json": {"total": 2, "items": [{"id": 1}]},
        "duration_ms": 12,
    }
    base.update(overrides)
    return Observation(**base)


def evaluate(assertion: Assertion, obs: Observation | None = None, operation_id: str = OPERATION):
    return AssertionEvaluator().evaluate(assertion, obs or observation(), operation_id)


def test_status_assertion_passes_and_fails() -> None:
    assert evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [200, 201]})).verdict is Verdict.PASSED
    assert evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [201]})).verdict is Verdict.FAILED


def test_json_path_operators() -> None:
    cases = [
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.total", equals=2), Verdict.PASSED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.total", type="integer"), Verdict.PASSED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.total", type="string"), Verdict.FAILED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.items", min_length=1), Verdict.PASSED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.items", max_length=0), Verdict.FAILED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.items[0].id", equals=1), Verdict.PASSED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.missing", exists=False), Verdict.PASSED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.missing", exists=True), Verdict.FAILED),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.missing", equals=1), Verdict.INCONCLUSIVE),
        (Assertion(kind=AssertionKind.JSON_PATH, path="$.total", length_equals=1), Verdict.FAILED),
    ]
    for assertion, expected in cases:
        assert evaluate(assertion).verdict is expected, assertion


def test_cross_field_comparison_operators() -> None:
    body = {"total": 2, "items": [{"id": 1}, {"id": 2}], "echo": 2}
    same = observation(body_json=body)
    assert (
        evaluate(Assertion(kind=AssertionKind.JSON_PATH, path="$.total", equals_path="$.echo"), same).verdict
        is Verdict.PASSED
    )
    length_ok = Assertion(kind=AssertionKind.JSON_PATH, path="$.items", length_equals_path="$.total")
    assert evaluate(length_ok, same).verdict is Verdict.PASSED

    mismatch = observation(body_json={"total": 3, "items": [{"id": 1}]})
    assert evaluate(length_ok, mismatch).verdict is Verdict.FAILED
    assert evaluate(length_ok, observation(body_json={"items": [{"id": 1}]})).verdict is Verdict.INCONCLUSIVE
    assert (
        evaluate(length_ok, observation(body_json={"total": "2", "items": [{"id": 1}]})).verdict
        is Verdict.INCONCLUSIVE
    )


def test_header_assertion_is_case_insensitive() -> None:
    assertion = Assertion(kind=AssertionKind.HEADER, name="content-type", contains="application/json")
    assert evaluate(assertion).verdict is Verdict.PASSED
    missing = Assertion(kind=AssertionKind.HEADER, name="x-trace", exists=False)
    assert evaluate(missing).verdict is Verdict.PASSED


def test_response_time_assertion() -> None:
    assertion = Assertion(kind=AssertionKind.RESPONSE_TIME_MS, max=100)
    assert evaluate(assertion).verdict is Verdict.PASSED
    assert evaluate(assertion, observation(duration_ms=101)).verdict is Verdict.FAILED


def test_non_json_body_is_inconclusive_not_passed() -> None:
    assertion = Assertion(kind=AssertionKind.JSON_PATH, path="$.total", type="integer")
    result = evaluate(assertion, observation(body_json=None, json_error="Expecting value"))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_request_failure_is_inconclusive() -> None:
    assertion = Assertion(kind=AssertionKind.STATUS, **{"in": [200]})
    result = evaluate(assertion, Observation(error="ConnectError: refused", status_code=None))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_json_schema_refers_to_a_declared_response(specification) -> None:
    """用例只声明"哪个 Operation 的哪个响应"，指针是实现细节。"""
    evaluator = AssertionEvaluator(specification)
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, response="200")
    good = observation(body_json={"items": [{"id": 1, "name": "Ada"}], "total": 1})
    assert evaluator.evaluate(assertion, good, OPERATION).verdict is Verdict.PASSED

    violating = observation(body_json={"items": [], "total": "two"})
    result = evaluator.evaluate(assertion, violating, OPERATION)
    assert result.verdict is Verdict.FAILED
    assert "total" in result.observed


def test_json_schema_resolves_nested_component_refs(specification) -> None:
    evaluator = AssertionEvaluator(specification)
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, response="200")
    body = {"id": 1, "name": "Ada", "owner": {"email": "a@example.com", "token": "x"}}
    assert evaluator.evaluate(assertion, observation(body_json=body), "getPetById").verdict is Verdict.PASSED
    bad = {"id": 1, "name": "Ada", "owner": {"email": 1}}
    assert evaluator.evaluate(assertion, observation(body_json=bad), "getPetById").verdict is Verdict.FAILED


def test_json_schema_without_specification_is_inconclusive() -> None:
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, response="200")
    assert evaluate(assertion).verdict is Verdict.INCONCLUSIVE


def test_unknown_response_code_is_inconclusive_not_passed(specification) -> None:
    evaluator = AssertionEvaluator(specification)
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, response="418")
    result = evaluator.evaluate(assertion, observation(), OPERATION)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert "未声明" in result.detail


def test_response_declared_without_json_schema_is_inconclusive(tmp_path: Path) -> None:
    from aprobe.specification import load_specification

    spec = tmp_path / "plain.yaml"
    spec.write_text(
        """
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /plain:
    get:
      operationId: getPlain
      responses:
        "200":
          description: 只声明了响应，没有 content
""",
        encoding="utf-8",
    )
    evaluator = AssertionEvaluator(load_specification(spec))
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, response="200")
    result = evaluator.evaluate(assertion, observation(), "getPlain")
    assert result.verdict is Verdict.INCONCLUSIVE
    assert "未声明 JSON schema" in result.detail


def test_response_schema_is_resolved_for_the_given_operation(specification) -> None:
    evaluator = AssertionEvaluator(specification)
    pet = Assertion(kind=AssertionKind.JSON_SCHEMA, response="201")
    assert evaluator.evaluate(pet, observation(body_json={"id": 3, "name": "Cid"}), "createPet").verdict is Verdict.PASSED
    assert evaluator.evaluate(pet, observation(body_json={"total": 2}), "createPet").verdict is Verdict.FAILED


def test_inline_schema_needs_no_specification() -> None:
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, **{"schema": {"type": "object"}})
    assert evaluate(assertion).verdict is Verdict.PASSED


def test_assertion_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.STATUS, **{"in": [200], "path": "$.a"})
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.STATUS)
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.JSON_PATH, path="$.a")
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.HEADER, name="x")
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.JSON_SCHEMA, response="200", **{"schema": {"type": "object"}})
    with pytest.raises(ValidationError):
        Assertion(kind=AssertionKind.JSON_SCHEMA, pointer="#/components/schemas/Pet")


def test_restricted_json_path_subset() -> None:
    hit, value = get_json_path({"a": {"b": [1, 2]}}, "$.a.b[1]")
    assert hit and value == 2
    hit, value = get_json_path({"a b": 1}, "$['a b']")
    assert hit and value == 1
    with pytest.raises(ValueError):
        get_json_path({"a": 1}, "a.b")
    with pytest.raises(ValueError):
        get_json_path({"a": 1}, "$..a")


def test_decide_verdict_prefers_failure_over_inconclusive() -> None:
    results = [
        evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [200]})),
        evaluate(Assertion(kind=AssertionKind.JSON_PATH, path="$.missing", equals=1)),
        evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [500]})),
    ]
    verdict, reason = decide_verdict(results)
    assert verdict is Verdict.FAILED
    assert reason.value == "completed"
