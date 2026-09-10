"""用例文件的读写与校验。

边界（ADR-0001）：用例文件就是审批载体。它进 Git、可 diff、可 review；
运行期禁止执行文件之外的请求，因此这里的校验必须把"看起来能跑但其实没约束"的用例挡在门外。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import CaseFileError
from .models import Operation, Specification, TestCase
from .specification import schema_has_path

#: 引用前序用例捕获值的写法
CAPTURE_PREFIX = "$captures."


def capture_references(case: TestCase) -> set[str]:
    """这条用例引用了哪些捕获变量。"""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, str):
            if node.startswith(CAPTURE_PREFIX):
                found.add(node[len(CAPTURE_PREFIX) :])
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(case.request.path_values)
    walk(case.request.query)
    walk(case.request.body)
    return found


def _transitive_requires(case: TestCase, by_id: dict[str, TestCase]) -> set[str]:
    collected: set[str] = set()
    pending = list(case.requires)
    while pending:
        current = pending.pop()
        if current in collected or current not in by_id:
            continue
        collected.add(current)
        pending.extend(by_id[current].requires)
    return collected

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    cases: list[TestCase] = Field(default_factory=list)


def load_case_file(path: str | Path) -> list[TestCase]:
    source = Path(path)
    if not source.is_file():
        raise CaseFileError(f"用例文件不存在: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseFileError(f"用例文件不是合法 YAML/JSON: {source}") from exc
    if not isinstance(raw, dict):
        raise CaseFileError(f"用例文件顶层必须是对象: {source}")
    try:
        return CaseFile.model_validate(raw).cases
    except ValidationError as exc:
        raise CaseFileError(f"用例文件结构非法: {source}\n{exc}") from exc


def dump_case_file(path: str | Path, cases: list[TestCase]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = CaseFile(cases=cases).model_dump(mode="json", exclude_none=True, by_alias=True)
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def operations_by_id(specification: Specification) -> dict[str, Operation]:
    return {operation.operation_id: operation for operation in specification.operations}


def validate_cases(cases: list[TestCase], specification: Specification | None = None) -> list[str]:
    """返回所有问题，而不是遇到第一个就退出——人需要一次看到全部。"""
    problems: list[str] = []
    seen: dict[str, int] = {}
    for index, case in enumerate(cases):
        if case.id in seen:
            problems.append(f"用例 id 重复: {case.id}（第 {seen[case.id] + 1} 与第 {index + 1} 条）")
        seen.setdefault(case.id, index)
        if not case.assertions:
            problems.append(f"{case.id}: 没有任何 Assertion，执行后只能得到「无法判定」")

    known = set(seen)
    for case in cases:
        for dependency in case.requires:
            if dependency not in known:
                problems.append(f"{case.id}: requires 指向不存在的用例 {dependency}")
        if case.id in case.requires:
            problems.append(f"{case.id}: requires 指向自己")
        verb = case.request.method.upper()
        if verb in READ_ONLY_METHODS and case.write:
            problems.append(f"{case.id}: {verb} 是只读方法，但用例声明 write=true")
        if verb not in READ_ONLY_METHODS and not case.write:
            problems.append(f"{case.id}: {verb} 是写方法，用例必须显式声明 write=true")
        if not re.match(r"^[a-z0-9][a-z0-9\-_.]{0,63}$", case.id):
            problems.append(f"{case.id}: 用例 id 只允许小写字母、数字、-_.，且不超过 64 字符")

    if _has_cycle(cases):
        problems.append("requires 之间存在环，无法确定执行顺序")

    # 链式用例（ADR-0004）：依赖与取值必须自洽。缺值时的正确行为是拒绝，不是编一个
    by_id = {case.id: case for case in cases}
    for case in cases:
        if case.creates_data and not case.write:
            problems.append(f"{case.id}: 声明了 creates_data 却 write=false，创建数据必须是写操作")
        available: set[str] = set()
        for dependency in _transitive_requires(case, by_id):
            available |= set(by_id[dependency].captures)
        for name in sorted(capture_references(case)):
            if name not in available:
                problems.append(
                    f"{case.id}: 引用了 $captures.{name}，但它不在任何前置用例（requires 闭包）的 captures 里"
                )

    if specification is None:
        return problems

    by_id = operations_by_id(specification)
    for case in cases:
        operation = by_id.get(case.operation_id)
        if operation is None:
            problems.append(f"{case.id}: 规范中不存在 operation_id={case.operation_id}")
            continue
        if case.request.method.upper() != operation.method:
            problems.append(
                f"{case.id}: 请求方法 {case.request.method} 与规范声明的 {operation.method} 不一致"
            )
        if case.request.path != operation.path:
            problems.append(
                f"{case.id}: 请求路径 {case.request.path} 与规范声明的 {operation.path} 不一致（规范可能已变更）"
            )
        required_path_params = {
            parameter.name for parameter in operation.parameters if parameter.location == "path" and parameter.required
        }
        provided = set(case.request.path_values or {})
        missing = sorted(required_path_params - provided)
        if missing:
            problems.append(f"{case.id}: 缺少必需的路径参数 {missing}")
        extra = sorted(provided - {parameter.name for parameter in operation.parameters if parameter.location == "path"})
        if extra:
            problems.append(f"{case.id}: 提供了未声明的路径参数 {extra}")
        for name, path in sorted(case.captures.items()):
            if not _capture_path_is_declared(specification, operation, path):
                problems.append(
                    f"{case.id}: 捕获 {name} 的路径 {path} 在 {operation.operation_id} "
                    "任何声明了 JSON 结构的成功响应里都找不到——不许凭空声称能取到这个值"
                )
        declared_statuses = {response.status for response in operation.responses}
        for assertion in case.assertions:
            if assertion.response is not None and assertion.response not in declared_statuses:
                problems.append(
                    f"{case.id}: 断言引用了 {operation.operation_id} 未声明的响应码 {assertion.response}"
                    f"（已声明：{sorted(declared_statuses)}）"
                )
    return problems


def _has_cycle(cases: list[TestCase]) -> bool:
    graph = {case.id: [dependency for dependency in case.requires if dependency in {c.id for c in cases}] for case in cases}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        for neighbour in graph.get(node, []):
            if visit(neighbour):
                return True
        state[node] = 2
        return False

    return any(visit(node) for node in graph)


def order_cases(cases: list[TestCase]) -> list[TestCase]:
    """按 requires 拓扑排序，同层按 id 字典序——顺序必须由确定性代码给出，不交给模型。"""
    by_id = {case.id: case for case in cases}
    remaining = {case.id: {dependency for dependency in case.requires if dependency in by_id} for case in cases}
    ordered: list[TestCase] = []
    while remaining:
        ready = sorted(node for node, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise CaseFileError("requires 之间存在环，无法排序")
        for node in ready:
            ordered.append(by_id[node])
            remaining.pop(node)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return ordered


def _capture_path_is_declared(specification: Specification, operation: Operation, path: str) -> bool:
    """这条取值路径是否存在于该 Operation 声明的某个成功响应里。"""
    for response in operation.responses:
        if response.status[:1] not in ("2", "3") or not response.schema_pointer:
            continue
        try:
            schema = specification.schema_document(response.schema_pointer)
        except ValueError:
            continue
        if schema_has_path(schema, path):
            return True
    return False
