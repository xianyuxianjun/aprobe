"""降级模式的确定性用例生成。

它不使用任何模型，因此可以在没有密钥的机器上跑通整条流程（CI、评估回归）。
边界：只生成"能安全构造请求"的用例；构造不出来的一律记入 needs_input，
绝不退化成"随便发一个 GET"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Assertion, AssertionKind, Operation, RequestSpec, Specification, TestCase

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class NeedsInput:
    operation_id: str
    method: str
    path: str
    reason: str


@dataclass
class GenerationResult:
    cases: list[TestCase] = field(default_factory=list)
    needs_input: list[NeedsInput] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def case_id_for(operation: Operation) -> str:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", operation.operation_id)
    slug = re.sub(r"[^a-z0-9]+", "-", camel_split.lower()).strip("-")
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", f"{operation.method}-{operation.path}".lower()).strip("-")
    return slug[:64] or "case"


def _declared_success(operation: Operation) -> list[str]:
    return sorted(
        (response.status for response in operation.responses if response.status[:1] == "2" and response.status.isdigit()),
        key=int,
    )


def generate_cases(specification: Specification, origin: str = "deterministic") -> GenerationResult:
    result = GenerationResult(ignored=list(specification.ignored))
    used_ids: set[str] = set()

    for operation in specification.operations:
        missing: list[str] = []
        if any(parameter.location == "path" and parameter.required for parameter in operation.parameters):
            missing.append("需要路径参数值")
        if any(parameter.location == "query" and parameter.required for parameter in operation.parameters):
            missing.append("需要必需的查询参数")
        if operation.request_body_required:
            missing.append("需要请求体")
        if missing:
            result.needs_input.append(
                NeedsInput(operation.operation_id, operation.method, operation.path, "、".join(missing))
            )
            continue

        success_codes = _declared_success(operation)
        if not success_codes:
            result.needs_input.append(
                NeedsInput(operation.operation_id, operation.method, operation.path, "未声明任何 2xx 响应，无法写出断言")
            )
            continue

        assertions = [Assertion(kind=AssertionKind.STATUS, **{"in": [int(code) for code in success_codes]})]
        json_response = next(
            (
                response
                for response in operation.responses
                if response.status in success_codes
                and response.media_type == "application/json"
                and response.schema_pointer
            ),
            None,
        )
        if json_response is not None and json_response.schema_pointer:
            assertions.append(Assertion(kind=AssertionKind.JSON_SCHEMA, response=json_response.status))

        base_id = case_id_for(operation)
        candidate = base_id
        suffix = 2
        while candidate in used_ids:
            candidate = f"{base_id[:60]}-{suffix}"
            suffix += 1
        used_ids.add(candidate)

        result.cases.append(
            TestCase(
                id=candidate,
                operation_id=operation.operation_id,
                summary=operation.summary or f"{operation.method} {operation.path} 的契约校验",
                request=RequestSpec(
                    method=operation.method,
                    path=operation.path,
                    path_values={},
                    query={},
                    headers={},
                    body=None,
                ),
                assertions=assertions,
                requires=[],
                write=operation.method not in READ_ONLY_METHODS,
                origin=origin,  # type: ignore[arg-type]
            )
        )

    result.cases.sort(key=lambda case: case.id)
    result.needs_input.sort(key=lambda item: item.operation_id)
    return result
