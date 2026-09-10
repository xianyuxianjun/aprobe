"""链式用例：让不可执行的接口变成可执行的（ADR-0004）。

这里要守住的核心不是"值能传下去"，而是**传下去的值不被信任**：
捕获值来自被测目标，它会流进下一个请求的路径/查询参数，
所以必须过和普通参数一样的校验，否则我们凭空开了一个注入面。
"""

from __future__ import annotations

import pytest
from conftest import write_config
from mock_service import MockService

from aprobe.assertions import AssertionEvaluator
from aprobe.cases import load_case_file, operations_by_id, validate_cases
from aprobe.cli import main
from aprobe.models import (
    Assertion,
    AssertionKind,
    RequestSpec,
    RunProvenance,
    TerminationReason,
    TestCase,
    Verdict,
)
from aprobe.policy import TargetPolicy
from aprobe.runner import TestRunner, resolve_captures
from aprobe.specification import load_specification


def status(code: int) -> Assertion:
    return Assertion(kind=AssertionKind.STATUS, **{"in": [code]})


def create_case(**overrides) -> TestCase:
    payload = dict(
        id="create-pet",
        operation_id="createPet",
        request=RequestSpec(method="POST", path="/pets", body={"name": "aprobe-probe-1"}),
        assertions=[status(201)],
        write=True,
        creates_data=True,
        captures={"pet_id": "$.id", "created_name": "$.name"},
        origin="agent",
    )
    payload.update(overrides)
    return TestCase(**payload)


def read_case(**overrides) -> TestCase:
    payload = dict(
        id="read-back",
        operation_id="getPetById",
        request=RequestSpec(method="GET", path="/pets/{petId}", path_values={"petId": "$captures.pet_id"}),
        assertions=[status(200)],
        requires=["create-pet"],
        origin="agent",
    )
    payload.update(overrides)
    return TestCase(**payload)


@pytest.fixture
def runner(specification) -> TestRunner:
    return TestRunner(
        TargetPolicy(allow=["127.0.0.1"], allow_write=True, timeout_ms=3000, max_response_bytes=65536),
        AssertionEvaluator(specification),
    )


# ---- 校验：缺值时的正确行为是拒绝，不是编一个 ----

def test_a_valid_chain_passes_validation(specification) -> None:
    assert validate_cases([create_case(), read_case()], specification) == []


def test_a_fabricated_capture_path_is_rejected(specification) -> None:
    """模型不许声称能从创建响应里取到一个契约里没有的字段。"""
    problems = validate_cases([create_case(captures={"pet_id": "$.does_not_exist"})], specification)
    assert any("不许凭空声称" in problem for problem in problems)


def test_a_reference_without_a_producer_is_rejected(specification) -> None:
    orphan = read_case(requires=[])
    assert any("不在任何前置用例" in problem for problem in validate_cases([orphan], specification))


def test_a_reference_produced_only_by_a_non_dependency_is_rejected(specification) -> None:
    other = create_case(id="unrelated-create")
    problems = validate_cases([other, read_case()], specification)
    assert any("不在任何前置用例" in problem for problem in problems)


def test_creating_data_without_declaring_write_is_rejected(specification) -> None:
    liar = create_case(write=False, requires=[])
    problems = validate_cases([liar], specification)
    assert any("写方法" in problem for problem in problems)


def test_capture_names_and_paths_are_constrained() -> None:
    with pytest.raises(Exception):
        create_case(captures={"Bad-Name": "$.id"})
    with pytest.raises(Exception):
        create_case(captures={"pet_id": "id"})


# ---- 执行：值真的传下去 ----

def test_the_captured_value_reaches_the_next_request(runner, specification, conformant) -> None:
    operations = operations_by_id(specification)
    provenance = RunProvenance(target=conformant.base_url, cases_file="-")
    first = runner.execute(create_case(), operations["createPet"], provenance, variables={})
    assert first.verdict is Verdict.PASSED
    assert first.captures == {"pet_id": "1", "created_name": "aprobe-probe-1"}

    second = runner.execute(read_case(), operations["getPetById"], provenance, variables=first.captures)
    assert second.request["url"] == f"{conformant.base_url}/pets/1"
    assert second.verdict is Verdict.PASSED


def test_write_read_consistency_is_assertable(runner, specification, conformant) -> None:
    """链式用例真正的价值：断言"写进去的东西能原样读回来"。"""
    operations = operations_by_id(specification)
    provenance = RunProvenance(target=conformant.base_url, cases_file="-")
    first = runner.execute(create_case(), operations["createPet"], provenance, variables={})
    verification = read_case(
        assertions=[
            status(200),
            Assertion(kind=AssertionKind.JSON_PATH, path="$.name", equals="aprobe-probe-1"),
        ]
    )
    second = runner.execute(verification, operations["getPetById"], provenance, variables=first.captures)
    assert second.verdict is Verdict.PASSED


def test_a_missing_capture_refuses_instead_of_sending_a_bad_request(runner, specification, conformant) -> None:
    operations = operations_by_id(specification)
    provenance = RunProvenance(target=conformant.base_url, cases_file="-")
    run = runner.execute(read_case(), operations["getPetById"], provenance, variables={})
    assert run.termination_reason is TerminationReason.POLICY_DENIED
    assert "还没有捕获到" in (run.observation.error or "")
    assert run.observation.status_code is None  # 请求根本没发出


def test_the_probe_slot_is_still_reproducible(conformant) -> None:
    """场景版本承诺"相同响应"：POST 写的是固定 id，所以重复运行不会漂。"""
    with MockService("conformant") as fresh:
        assert fresh.behaviour.scenario == conformant.behaviour.scenario


# ---- 安全：捕获值来自被测目标，必须当不可信输入 ----

def test_a_captured_value_is_validated_like_any_other_parameter(runner, specification, conformant) -> None:
    """目标返回的内容不能因为"来自捕获"就绕过参数校验。"""
    operations = operations_by_id(specification)
    provenance = RunProvenance(target=conformant.base_url, cases_file="-")
    hostile = create_case(
        id="create-hostile",
        request=RequestSpec(method="POST", path="/pets", body={"name": "../../etc/passwd"}),
        captures={"pet_id": "$.name"},  # 目标会把我们写进去的恶意串原样返回
    )
    first = runner.execute(hostile, operations["createPet"], provenance, variables={})
    assert first.captures == {"pet_id": "../../etc/passwd"}

    second = runner.execute(read_case(), operations["getPetById"], provenance, variables=first.captures)
    assert second.termination_reason is TerminationReason.POLICY_DENIED
    assert "路径穿越" in (second.observation.error or "")
    assert second.observation.status_code is None


def test_a_redacted_capture_is_refused_rather_than_sent(runner, specification, conformant) -> None:
    """捕获到敏感字段时它是 [REDACTED]；把占位符发出去只会制造一个假成功的请求。"""
    operations = operations_by_id(specification)
    provenance = RunProvenance(target=conformant.base_url, cases_file="-")
    # getPetById 的 200 响应里有 owner.token，脱敏后捕获到的就是 [REDACTED]
    leaker = TestCase(
        id="read-a-token",
        operation_id="getPetById",
        request=RequestSpec(method="GET", path="/pets/{petId}", path_values={"petId": "1"}),
        assertions=[status(200)],
        captures={"token": "$.owner.token"},
        origin="agent",
    )
    first = runner.execute(leaker, operations["getPetById"], provenance, variables={})
    assert first.captures == {"token": "[REDACTED]"}

    consumer = TestCase(
        id="use-the-token",
        operation_id="getPetById",
        request=RequestSpec(method="GET", path="/pets/{petId}", path_values={"petId": "$captures.token"}),
        assertions=[status(200)],
        requires=["read-a-token"],
        origin="agent",
    )
    second = runner.execute(consumer, operations["getPetById"], provenance, variables=first.captures)
    assert second.termination_reason is TerminationReason.POLICY_DENIED
    assert "脱敏" in (second.observation.error or "")


def test_resolve_captures_rejects_control_characters() -> None:
    with pytest.raises(Exception):
        resolve_captures("$captures.x", {"x": "bad\nvalue"}, "查询参数 q")


# ---- CLI：变量袋只活在这一次运行里 ----

def test_cli_runs_a_chain_and_threads_the_variables(tmp_path, spec_path, conformant, capsys) -> None:
    cases_file = tmp_path / "chain.yaml"
    payload = {
        "version": 1,
        "cases": [
            create_case().model_dump(mode="json", exclude_none=True, by_alias=True),
            read_case().model_dump(mode="json", exclude_none=True, by_alias=True),
        ],
    }
    import yaml

    cases_file.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = write_config(
        tmp_path, base_url=conformant.base_url, spec=spec_path, cases=cases_file, allow_write=True
    )

    assert main(["run", "--config", str(config)]) == 0
    output = capsys.readouterr().out
    assert "create-pet" in output and "read-back" in output
    assert "合计 2：通过 2" in output

    loaded = load_case_file(cases_file)
    assert validate_cases(loaded, load_specification(spec_path)) == []


def test_the_chain_is_refused_without_allow_write(tmp_path, spec_path, conformant, capsys) -> None:
    """创建数据这件事必须由人显式开启。"""
    import yaml

    cases_file = tmp_path / "chain.yaml"
    cases_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "cases": [
                    create_case().model_dump(mode="json", exclude_none=True, by_alias=True),
                    read_case().model_dump(mode="json", exclude_none=True, by_alias=True),
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = write_config(
        tmp_path, base_url=conformant.base_url, spec=spec_path, cases=cases_file, allow_write=False
    )
    assert main(["run", "--config", str(config)]) == 4  # policy denied
    # 拒绝的理由必须出现在输出里，否则"策略拒绝"等于没说
    assert "未显式开启写操作" in capsys.readouterr().out
