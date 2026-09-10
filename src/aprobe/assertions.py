"""Assertion 的确定性求值。

边界：这里绝不抛异常、绝不调用模型。无法求值的断言只能得到「无法判定」，
不允许退化成「通过」。JSONPath 只支持受限子集：`$`、`.key`、`['key']`、`[n]`。
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .models import Assertion, AssertionKind, AssertionResult, Observation, Specification, Verdict

_MISSING = object()

_TYPE_CHECKS: dict[str, Any] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
    "null": lambda value: value is None,
}


def get_json_path(document: Any, path: str) -> tuple[bool, Any]:
    """受限 JSONPath 取值。返回 (是否命中, 值)。"""
    if not path.startswith("$"):
        raise ValueError(f"JSONPath 必须以 $ 开头: {path!r}")
    current = document
    index = 1
    while index < len(path):
        char = path[index]
        if char == ".":
            end = index + 1
            while end < len(path) and path[end] not in ".[":
                end += 1
            key = path[index + 1 : end]
            if not key:
                raise ValueError(f"JSONPath 段为空: {path!r}")
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
            index = end
        elif char == "[":
            end = path.find("]", index)
            if end == -1:
                raise ValueError(f"JSONPath 缺少 ]: {path!r}")
            token = path[index + 1 : end].strip()
            if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
                key = token[1:-1]
                if not isinstance(current, dict) or key not in current:
                    return False, None
                current = current[key]
            else:
                if not isinstance(current, list):
                    return False, None
                try:
                    current = current[int(token)]
                except (ValueError, IndexError):
                    return False, None
            index = end + 1
        else:
            raise ValueError(f"JSONPath 含不支持的语法: {path!r}")
    return True, current


def _describe(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


class AssertionEvaluator:
    """求值器。持有 Specification 以便解析 json_schema 断言引用的契约片段。"""

    def __init__(self, specification: Specification | None = None) -> None:
        self._specification = specification

    def evaluate_all(
        self, assertions: list[Assertion], observation: Observation, operation_id: str
    ) -> list[AssertionResult]:
        return [self.evaluate(assertion, observation, operation_id) for assertion in assertions]

    def evaluate(self, assertion: Assertion, observation: Observation, operation_id: str) -> AssertionResult:
        try:
            return self._dispatch(assertion, observation, operation_id)
        except Exception as exc:  # 求值失败只能是「无法判定」，不允许变成通过
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="求值失败",
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _dispatch(self, assertion: Assertion, observation: Observation, operation_id: str) -> AssertionResult:
        if observation.error and observation.status_code is None:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="未收到响应",
                detail=observation.error,
            )
        if assertion.kind is AssertionKind.STATUS:
            return self._status(assertion, observation)
        if assertion.kind is AssertionKind.JSON_SCHEMA:
            return self._json_schema(assertion, observation, operation_id)
        if assertion.kind is AssertionKind.JSON_PATH:
            return self._json_path(assertion, observation)
        if assertion.kind is AssertionKind.HEADER:
            return self._header(assertion, observation)
        return self._response_time(assertion, observation)

    def _status(self, assertion: Assertion, observation: Observation) -> AssertionResult:
        expected = assertion.in_ or []
        actual = observation.status_code
        verdict = Verdict.PASSED if actual in expected else Verdict.FAILED
        return AssertionResult(
            assertion=assertion,
            verdict=verdict,
            observed=f"status={actual}",
            detail=f"期望之一 {expected}",
        )

    def _json_schema(self, assertion: Assertion, observation: Observation, operation_id: str) -> AssertionResult:
        if observation.body_json is None:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="响应体不是 JSON",
                detail=observation.json_error or "响应体为空或无法解析为 JSON",
            )
        if assertion.response is not None:
            if self._specification is None:
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.INCONCLUSIVE,
                    observed="缺少规范上下文",
                    detail=f"无法解析 {operation_id} 的 {assertion.response} 响应",
                )
            try:
                schema = self._specification.response_schema(operation_id, assertion.response)
            except ValueError as exc:
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.INCONCLUSIVE,
                    observed="契约片段无法解析",
                    detail=str(exc),
                )
        else:
            schema = assertion.schema_
        if not isinstance(schema, dict):
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="契约片段不是对象",
                detail=_describe(schema),
            )
        try:
            validator = Draft202012Validator(schema)
        except SchemaError as exc:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="契约本身非法",
                detail=str(exc),
            )
        errors = sorted(validator.iter_errors(observation.body_json), key=lambda item: list(item.path))
        if not errors:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.PASSED,
                observed="响应体符合契约",
                detail=f"{operation_id} 的 {assertion.response} 响应" if assertion.response else "内联 schema",
            )
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "(root)"
        return AssertionResult(
            assertion=assertion,
            verdict=Verdict.FAILED,
            observed=f"契约校验失败于 {location}",
            detail=first.message,
        )

    def _json_path(self, assertion: Assertion, observation: Observation) -> AssertionResult:
        if observation.body_json is None:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed="响应体不是 JSON",
                detail=observation.json_error or "响应体为空或无法解析为 JSON",
            )
        hit, value = get_json_path(observation.body_json, assertion.path or "$")
        if not hit:
            if assertion.exists is False:
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.PASSED,
                    observed=f"{assertion.path} 不存在",
                    detail="符合 exists=false",
                )
            verdict = Verdict.FAILED if assertion.exists is True else Verdict.INCONCLUSIVE
            return AssertionResult(
                assertion=assertion,
                verdict=verdict,
                observed=f"{assertion.path} 未命中",
                detail="取值路径不存在" if verdict is Verdict.INCONCLUSIVE else "期望字段存在",
            )

        checks: list[tuple[str, bool]] = []
        if assertion.exists is not None:
            checks.append((f"exists={assertion.exists}", assertion.exists is True))
        if assertion.equals is not None:
            checks.append((f"equals {_describe(assertion.equals)}", value == assertion.equals))
        if assertion.equals_path is not None:
            other_hit, other = get_json_path(observation.body_json, assertion.equals_path)
            if not other_hit:
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.INCONCLUSIVE,
                    observed=f"{assertion.equals_path} 未命中",
                    detail="跨字段比较缺少参照值",
                )
            checks.append((f"equals {assertion.equals_path} ({_describe(other)})", value == other))
        if assertion.length_equals_path is not None:
            other_hit, other = get_json_path(observation.body_json, assertion.length_equals_path)
            if not other_hit or not isinstance(other, int) or isinstance(other, bool):
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.INCONCLUSIVE,
                    observed=_describe(other) if other_hit else f"{assertion.length_equals_path} 未命中",
                    detail=f"{assertion.length_equals_path} 必须取到整数才能作为长度参照",
                )
            checks.append(
                (
                    f"length == {assertion.length_equals_path} ({other})",
                    hasattr(value, "__len__") and len(value) == other,
                )
            )
        if assertion.type is not None:
            predicate = _TYPE_CHECKS.get(assertion.type)
            if predicate is None:
                return AssertionResult(
                    assertion=assertion,
                    verdict=Verdict.INCONCLUSIVE,
                    observed=f"未知类型 {assertion.type}",
                    detail=f"支持: {sorted(_TYPE_CHECKS)}",
                )
            checks.append((f"type={assertion.type}", bool(predicate(value))))
        if assertion.contains is not None:
            if isinstance(value, str):
                contained = isinstance(assertion.contains, str) and assertion.contains in value
            elif isinstance(value, list):
                contained = assertion.contains in value
            elif isinstance(value, dict):
                contained = assertion.contains in value
            else:
                contained = False
            checks.append((f"contains {_describe(assertion.contains)}", contained))
        if assertion.length_equals is not None:
            checks.append((f"length={assertion.length_equals}", hasattr(value, "__len__") and len(value) == assertion.length_equals))
        if assertion.min_length is not None:
            checks.append((f"min_length={assertion.min_length}", hasattr(value, "__len__") and len(value) >= assertion.min_length))
        if assertion.max_length is not None:
            checks.append((f"max_length={assertion.max_length}", hasattr(value, "__len__") and len(value) <= assertion.max_length))

        if not checks:
            return AssertionResult(
                assertion=assertion,
                verdict=Verdict.INCONCLUSIVE,
                observed=_describe(value),
                detail="该 json_path 断言没有任何可求值的算子",
            )
        failures = [label for label, ok in checks if not ok]
        return AssertionResult(
            assertion=assertion,
            verdict=Verdict.FAILED if failures else Verdict.PASSED,
            observed=f"{assertion.path} = {_describe(value)}",
            detail="; ".join(failures) if failures else "; ".join(label for label, _ in checks),
        )

    def _header(self, assertion: Assertion, observation: Observation) -> AssertionResult:
        wanted = (assertion.name or "").lower()
        actual = next((value for key, value in observation.headers.items() if key.lower() == wanted), None)
        if actual is None:
            if assertion.exists is False:
                return AssertionResult(
                    assertion=assertion, verdict=Verdict.PASSED, observed=f"{wanted} 不存在", detail="符合 exists=false"
                )
            verdict = Verdict.FAILED if assertion.exists is True or assertion.equals is not None or assertion.contains is not None else Verdict.INCONCLUSIVE
            return AssertionResult(
                assertion=assertion, verdict=verdict, observed=f"{wanted} 不存在", detail="期望响应头存在"
            )
        checks: list[tuple[str, bool]] = []
        if assertion.exists is not None:
            checks.append((f"exists={assertion.exists}", assertion.exists is True))
        if assertion.equals is not None:
            checks.append((f"equals {assertion.equals!r}", actual == assertion.equals))
        if assertion.contains is not None:
            checks.append((f"contains {assertion.contains!r}", isinstance(assertion.contains, str) and assertion.contains in actual))
        failures = [label for label, ok in checks if not ok]
        return AssertionResult(
            assertion=assertion,
            verdict=Verdict.FAILED if failures else Verdict.PASSED,
            observed=f"{wanted}: {actual[:120]}",
            detail="; ".join(failures) if failures else "; ".join(label for label, _ in checks),
        )

    def _response_time(self, assertion: Assertion, observation: Observation) -> AssertionResult:
        if observation.duration_ms is None:
            return AssertionResult(
                assertion=assertion, verdict=Verdict.INCONCLUSIVE, observed="未测量耗时", detail=""
            )
        limit = assertion.max or 0
        return AssertionResult(
            assertion=assertion,
            verdict=Verdict.PASSED if observation.duration_ms <= limit else Verdict.FAILED,
            observed=f"{observation.duration_ms}ms",
            detail=f"上限 {limit}ms",
        )


def decide_verdict(results: list[AssertionResult]) -> tuple[Verdict, Any]:
    """由断言结果合成 Test Run 结论：失败优先，其次无法判定，最后才是通过。"""
    from .models import TerminationReason

    if not results:
        return Verdict.INCONCLUSIVE, TerminationReason.UNEVALUABLE
    if any(result.verdict is Verdict.FAILED for result in results):
        return Verdict.FAILED, TerminationReason.COMPLETED
    if any(result.verdict is Verdict.INCONCLUSIVE for result in results):
        return Verdict.INCONCLUSIVE, TerminationReason.UNEVALUABLE
    return Verdict.PASSED, TerminationReason.COMPLETED
