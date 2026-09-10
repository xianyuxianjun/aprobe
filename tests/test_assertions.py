from __future__ import annotations

import pytest
from pydantic import ValidationError

from aprobe.assertions import AssertionEvaluator, decide_verdict, get_json_path
from aprobe.models import Assertion, AssertionKind, Observation, Verdict


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


def test_status_assertion_passes_and_fails() -> None:
    evaluator = AssertionEvaluator()
    passed = evaluator.evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [200, 201]}), observation())
    assert passed.verdict is Verdict.PASSED
    failed = evaluator.evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [201]}), observation())
    assert failed.verdict is Verdict.FAILED


def test_json_path_operators() -> None:
    evaluator = AssertionEvaluator()
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
        assert evaluator.evaluate(assertion, observation()).verdict is expected, assertion


def test_cross_field_comparison_operators() -> None:
    evaluator = AssertionEvaluator()
    body = {"total": 2, "items": [{"id": 1}, {"id": 2}], "echo": 2}
    same = observation(body_json=body)
    assert (
        evaluator.evaluate(Assertion(kind=AssertionKind.JSON_PATH, path="$.total", equals_path="$.echo"), same).verdict
        is Verdict.PASSED
    )
    length_ok = Assertion(kind=AssertionKind.JSON_PATH, path="$.items", length_equals_path="$.total")
    assert evaluator.evaluate(length_ok, same).verdict is Verdict.PASSED

    mismatch = observation(body_json={"total": 3, "items": [{"id": 1}]})
    result = evaluator.evaluate(length_ok, mismatch)
    assert result.verdict is Verdict.FAILED

    reference_missing = observation(body_json={"items": [{"id": 1}]})
    assert evaluator.evaluate(length_ok, reference_missing).verdict is Verdict.INCONCLUSIVE

    not_an_integer = observation(body_json={"total": "2", "items": [{"id": 1}]})
    assert evaluator.evaluate(length_ok, not_an_integer).verdict is Verdict.INCONCLUSIVE


def test_header_assertion_is_case_insensitive() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.HEADER, name="content-type", contains="application/json")
    assert evaluator.evaluate(assertion, observation()).verdict is Verdict.PASSED
    missing = Assertion(kind=AssertionKind.HEADER, name="x-trace", exists=False)
    assert evaluator.evaluate(missing, observation()).verdict is Verdict.PASSED


def test_response_time_assertion() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.RESPONSE_TIME_MS, max=100)
    assert evaluator.evaluate(assertion, observation()).verdict is Verdict.PASSED
    assert evaluator.evaluate(assertion, observation(duration_ms=101)).verdict is Verdict.FAILED


def test_non_json_body_is_inconclusive_not_passed() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.JSON_PATH, path="$.total", type="integer")
    result = evaluator.evaluate(assertion, observation(body_json=None, json_error="Expecting value"))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_request_failure_is_inconclusive() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.STATUS, **{"in": [200]})
    result = evaluator.evaluate(assertion, Observation(error="ConnectError: refused", status_code=None))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_json_schema_against_contract(specification) -> None:
    evaluator = AssertionEvaluator(specification)
    pointer = "#/paths/~1pets/get/responses/200/content/application~1json/schema"
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, pointer=pointer)
    good = observation(body_json={"items": [{"id": 1, "name": "Ada"}], "total": 1})
    assert evaluator.evaluate(assertion, good).verdict is Verdict.PASSED

    violating = observation(body_json={"items": [], "total": "two"})
    result = evaluator.evaluate(assertion, violating)
    assert result.verdict is Verdict.FAILED
    assert "total" in result.observed


def test_json_schema_resolves_nested_component_refs(specification) -> None:
    evaluator = AssertionEvaluator(specification)
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, pointer="#/components/schemas/Pet")
    body = {"id": 1, "name": "Ada", "owner": {"email": "a@example.com", "token": "x"}}
    assert evaluator.evaluate(assertion, observation(body_json=body)).verdict is Verdict.PASSED
    bad = {"id": 1, "name": "Ada", "owner": {"email": 1}}
    assert evaluator.evaluate(assertion, observation(body_json=bad)).verdict is Verdict.FAILED


def test_json_schema_without_specification_is_inconclusive() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, pointer="#/components/schemas/Pet")
    assert evaluator.evaluate(assertion, observation()).verdict is Verdict.INCONCLUSIVE


def test_unresolvable_pointer_is_inconclusive() -> None:
    evaluator = AssertionEvaluator()
    assertion = Assertion(kind=AssertionKind.JSON_SCHEMA, pointer="#/components/schemas/Nope")
    assert evaluator.evaluate(assertion, observation()).verdict is Verdict.INCONCLUSIVE


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
        Assertion(kind=AssertionKind.JSON_SCHEMA, pointer="#/a", **{"schema": {"type": "object"}})


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
    evaluator = AssertionEvaluator()
    results = [
        evaluator.evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [200]}), observation()),
        evaluator.evaluate(Assertion(kind=AssertionKind.JSON_PATH, path="$.missing", equals=1), observation()),
        evaluator.evaluate(Assertion(kind=AssertionKind.STATUS, **{"in": [500]}), observation()),
    ]
    verdict, reason = decide_verdict(results)
    assert verdict is Verdict.FAILED
    assert reason.value == "completed"
