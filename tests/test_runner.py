from __future__ import annotations

import socket

import pytest

from aprobe.assertions import AssertionEvaluator
from aprobe.cases import load_case_file, operations_by_id
from aprobe.errors import PolicyDeniedError
from aprobe.models import (
    Assertion,
    AssertionKind,
    RequestSpec,
    TerminationReason,
    TestCase,
    Verdict,
)
from aprobe.policy import TargetPolicy
from aprobe.runner import CredentialProvider, TestRunner, build_request


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_runner(specification, *, allow, allow_write=False, credentials=None, environ=None, max_bytes=65536) -> TestRunner:
    return TestRunner(
        policy=TargetPolicy(allow=allow, allow_write=allow_write, max_response_bytes=max_bytes),
        evaluator=AssertionEvaluator(specification),
        credentials=credentials or CredentialProvider(),
        environ=environ or {},
    )


def case(case_id: str, operation_id: str, method: str, path: str, *, path_values=None, assertions=None, write=False) -> TestCase:
    return TestCase(
        id=case_id,
        operation_id=operation_id,
        request=RequestSpec(method=method, path=path, path_values=path_values or {}),
        assertions=assertions
        or [Assertion(kind=AssertionKind.STATUS, **{"in": [200]})],
        write=write,
    )


def test_conformant_run_passes_and_records_evidence(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case("list-pets", "listPets", "GET", "/pets"),
        operations["listPets"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.verdict is Verdict.PASSED
    assert run.termination_reason is TerminationReason.COMPLETED
    assert run.observation.status_code == 200
    assert run.request["url"] == f"{conformant.base_url}/pets"
    assert run.duration_ms >= 0


def test_contract_violation_is_detected(specification, violating) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case(
            "get-pet-stats",
            "getPetStats",
            "GET",
            "/pets/stats",
            assertions=[
                Assertion(kind=AssertionKind.STATUS, **{"in": [200]}),
                Assertion(
                    kind=AssertionKind.JSON_SCHEMA,
                    pointer="#/paths/~1pets~1stats/get/responses/200/content/application~1json/schema",
                ),
            ],
        ),
        operations["getPetStats"],
        violating.base_url,
        specification.source,
        specification.version,
    )
    assert run.verdict is Verdict.FAILED
    assert any(result.verdict is Verdict.FAILED for result in run.assertion_results)


def test_secret_in_response_is_redacted_before_recording(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case("get-pet-by-id", "getPetById", "GET", "/pets/{petId}", path_values={"petId": "1"}),
        operations["getPetById"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.verdict is Verdict.PASSED
    assert "mock-secret-token" not in run.observation.body_text
    assert "[REDACTED]" in run.observation.body_text


def test_credential_comes_from_environment_and_is_redacted(specification, conformant) -> None:
    operations = operations_by_id(specification)
    without = make_runner(specification, allow=["127.0.0.1"])
    run = without.execute(
        case(
            "owners",
            "getPetOwner",
            "GET",
            "/pets/{petId}/owners",
            path_values={"petId": "1"},
            assertions=[Assertion(kind=AssertionKind.STATUS, **{"in": [200, 401]})],
        ),
        operations["getPetOwner"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.observation.status_code == 401

    with_token = make_runner(
        specification,
        allow=["127.0.0.1"],
        credentials=CredentialProvider("bearer", "APROBE_TEST_TOKEN"),
        environ={"APROBE_TEST_TOKEN": "demo-token"},
    )
    authorized = with_token.execute(
        case(
            "owners",
            "getPetOwner",
            "GET",
            "/pets/{petId}/owners",
            path_values={"petId": "1"},
            assertions=[Assertion(kind=AssertionKind.STATUS, **{"in": [200, 401]})],
        ),
        operations["getPetOwner"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert authorized.observation.status_code == 200
    assert authorized.request["headers"]["Authorization"] == "[REDACTED]"
    assert "demo-token" not in authorized.model_dump_json()


def test_disallowed_target_never_leaves_the_process(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["example.com"])
    run = runner.execute(
        case("list-pets", "listPets", "GET", "/pets"),
        operations["listPets"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.verdict is Verdict.INCONCLUSIVE
    assert run.termination_reason is TerminationReason.POLICY_DENIED
    assert run.observation.status_code is None


def test_write_case_is_blocked_by_default(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case("create-pet", "createPet", "POST", "/pets", write=True),
        operations["createPet"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.termination_reason is TerminationReason.POLICY_DENIED


def test_unsafe_path_value_is_rejected_before_any_request(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case("bad", "getPetById", "GET", "/pets/{petId}", path_values={"petId": "../../etc/passwd"}),
        operations["getPetById"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.termination_reason is TerminationReason.POLICY_DENIED
    assert "路径穿越" in (run.observation.error or "")


def test_connection_failure_is_inconclusive(specification) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    run = runner.execute(
        case("list-pets", "listPets", "GET", "/pets"),
        operations["listPets"],
        f"http://127.0.0.1:{closed_port()}",
        specification.source,
        specification.version,
    )
    assert run.verdict is Verdict.INCONCLUSIVE
    assert run.termination_reason is TerminationReason.REQUEST_FAILED


def test_system_proxy_is_not_trusted(specification, monkeypatch) -> None:
    """允许范围必须是唯一的访问控制：代理不得把请求转走。

    充当"代理"的 Mock 会对任意路径返回 200，因此一旦请求被转发，
    结论就会变成通过——这正是这个测试要防住的情况。
    """
    from mock_service import MockService

    operations = operations_by_id(specification)
    proxy = MockService("conformant").start()
    try:
        monkeypatch.setenv("HTTP_PROXY", proxy.base_url)
        monkeypatch.setenv("ALL_PROXY", proxy.base_url)
        runner = make_runner(specification, allow=["127.0.0.1"])
        run = runner.execute(
            case("list-pets", "listPets", "GET", "/pets"),
            operations["listPets"],
            f"http://127.0.0.1:{closed_port()}",
            specification.source,
            specification.version,
        )
        assert run.termination_reason is TerminationReason.REQUEST_FAILED
        assert run.observation.status_code is None
    finally:
        proxy.stop()


def test_truncated_response_cannot_silently_pass(specification, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"], max_bytes=10)
    run = runner.execute(
        case(
            "list-pets",
            "listPets",
            "GET",
            "/pets",
            assertions=[
                Assertion(kind=AssertionKind.STATUS, **{"in": [200]}),
                Assertion(kind=AssertionKind.JSON_PATH, path="$.total", type="integer"),
            ],
        ),
        operations["listPets"],
        conformant.base_url,
        specification.source,
        specification.version,
    )
    assert run.observation.truncated is True
    assert run.verdict is Verdict.INCONCLUSIVE


def test_build_request_refuses_undeclared_path_parameter(specification) -> None:
    operations = operations_by_id(specification)
    with pytest.raises(PolicyDeniedError, match="不在 Operation 声明的路径中"):
        build_request(
            case("x", "getPetById", "GET", "/pets/{petId}", path_values={"petId": "1", "evil": "1"}),
            operations["getPetById"],
            "http://127.0.0.1:8080",
        )


def test_committed_cases_all_pass_against_conformant_baseline(specification, cases_path, conformant) -> None:
    operations = operations_by_id(specification)
    runner = make_runner(specification, allow=["127.0.0.1"])
    cases = load_case_file(cases_path)
    verdicts = {
        item.id: runner.execute(
            item, operations[item.operation_id], conformant.base_url, specification.source, specification.version
        ).verdict
        for item in cases
    }
    assert set(verdicts) == {
        "get-health",
        "get-pet-by-id",
        "get-pet-by-id-not-found",
        "get-pet-owner-without-credential",
        "get-pet-stats",
        "list-pets",
        "list-pets-total-matches-items",
    }
    assert all(verdict is Verdict.PASSED for verdict in verdicts.values()), verdicts
