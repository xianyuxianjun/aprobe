"""唯一的网络出口。

边界：请求只能由「Operation 声明的路径模板 + 用例提供的参数值」构造，
路径本身不可由用例自定义；凭据由配置注入，绝不出现在用例文件里。
策略检查在请求发出之前完成。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .assertions import AssertionEvaluator, decide_verdict, get_json_path
from .cases import CAPTURE_PREFIX
from .errors import ConfigError, PolicyDeniedError
from .models import Operation, Observation, RunProvenance, TestCase, TestRun, Verdict
from .policy import SAFE_PARAM_NAME, TargetPolicy, forbidden_header_reason, value_problem
from .sanitizer import REDACTED, sanitize_headers, sanitize_text, sanitize_value

@dataclass
class BuiltRequest:
    url: str
    method: str
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any | None = None


def _check_value(label: str, value: Any) -> str:
    """把 policy 的规则变成拒绝。规则本身只有一处定义。"""
    problem = value_problem(label, value)
    if problem:
        raise PolicyDeniedError(problem)
    return str(value)


class CredentialProvider:
    """凭据值只从环境变量读取。值不落库、不写日志、不进报告。"""

    def __init__(self, scheme: str | None = None, env_var: str | None = None, header: str | None = None) -> None:
        self.scheme = (scheme or "").lower()
        self.env_var = env_var
        self.header = header or "Authorization"

    @property
    def configured(self) -> bool:
        return bool(self.env_var)

    def headers(self, environ: dict[str, str]) -> dict[str, str]:
        if not self.env_var:
            return {}
        value = environ.get(self.env_var)
        if not value:
            return {}
        if self.scheme == "bearer":
            return {self.header: f"Bearer {value}"}
        if self.scheme == "basic":
            return {self.header: f"Basic {value}"}
        if self.scheme == "api-key":
            return {self.header: value}
        return {self.header: value}


def resolve_captures(value: Any, variables: dict[str, str], label: str) -> Any:
    """把 `$captures.X` 替换成前序用例捕获到的值。

    捕获值来自**被测目标**，它会流进下一个请求的路径/查询/请求体，所以必须和普通
    参数走同一套校验（长度、控制字符、路径穿越）——否则我们等于把目标返回的内容
    当成了可信输入，凭空开了一个注入面。

    脱敏值一律拒绝：把它当真值发出去只会制造一个看起来成功、实际无意义的请求。
    """
    if isinstance(value, str) and value.startswith(CAPTURE_PREFIX):
        name = value[len(CAPTURE_PREFIX) :]
        if name not in variables:
            raise PolicyDeniedError(f"{label} 引用了 $captures.{name}，但本次运行还没有捕获到它")
        captured = variables[name]
        if captured == REDACTED or captured == "":
            raise PolicyDeniedError(
                f"{label} 引用的 $captures.{name} 是脱敏后的值，不能作为请求参数发出（ADR-0004）"
            )
        return _check_value(label, captured)
    if isinstance(value, dict):
        return {key: resolve_captures(item, variables, f"{label}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_captures(item, variables, f"{label}[{index}]") for index, item in enumerate(value)]
    return value


def build_request(
    case: TestCase, operation: Operation, base_url: str, variables: dict[str, str] | None = None
) -> BuiltRequest:
    """从 Operation 的路径模板构造请求。用例只能提供值，不能提供路径。"""
    variables = dict(variables or {})
    path = operation.path
    for name, raw in (case.request.path_values or {}).items():
        value = resolve_captures(raw, variables, f"路径参数 {name}")
        placeholder = "{" + name + "}"
        if placeholder not in path:
            raise PolicyDeniedError(f"路径参数 {name} 不在 Operation 声明的路径中")
        path = path.replace(placeholder, _check_value(f"路径参数 {name}", value))
    if "{" in path:
        missing = sorted(set(re.findall(r"\{([^}]+)\}", path)))
        raise PolicyDeniedError(f"路径参数未全部提供: {missing}")

    query_pairs: list[tuple[str, str]] = []
    for name, raw in (case.request.query or {}).items():
        value = resolve_captures(raw, variables, f"查询参数 {name}")
        if not SAFE_PARAM_NAME.match(str(name)):
            raise PolicyDeniedError(f"查询参数名非法: {name!r}")
        if isinstance(value, (list, tuple)):
            query_pairs.extend((str(name), _check_value(f"查询参数 {name}", item)) for item in value)
        else:
            query_pairs.append((str(name), _check_value(f"查询参数 {name}", value)))

    headers: dict[str, str] = {}
    for name, value in (case.request.headers or {}).items():
        problem = forbidden_header_reason(str(name))
        if problem:
            raise PolicyDeniedError(problem)
        headers[str(name)] = _check_value(f"请求头 {name}", value)
    headers.setdefault("Accept", "application/json")

    url = base_url.rstrip("/") + path
    if query_pairs:
        url += "?" + "&".join(f"{name}={value}" for name, value in query_pairs)

    body = resolve_captures(case.request.body, variables, "请求体")
    if body is not None and not isinstance(body, (dict, list)):
        raise PolicyDeniedError("请求体只支持 JSON 对象或数组")
    return BuiltRequest(url=url, method=case.request.method.upper(), headers=headers, json_body=body)


def probe_baseline(policy: TargetPolicy, base_url: str, timeout_ms: int = 3000) -> dict[str, Any]:
    """读取评估基准的自述身份。

    这不是 TestRun：它不测接口，只确认“我们量的到底是哪个基准”。
    它仍然走同一套策略检查与同一套不信任代理的约束。
    """
    url = base_url.rstrip("/") + "/__scenario"
    decision = policy.check(url=url, method="GET")
    if not decision.allowed:
        raise PolicyDeniedError(decision.reason)
    try:
        response = httpx.get(url, timeout=timeout_ms / 1000, follow_redirects=False, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        # 基准不可达不是“测试失败”，而是“这次度量无意义”：它必须区别于断言失败
        raise ConfigError(f"无法确认评估基准：{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"评估基准返回的不是 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("评估基准返回的不是对象")
    return payload


class TestRunner:
    """执行一条 Test Case，返回一条可回放的 Test Run。它从不抛异常，也从不做判定之外的推断。"""

    __test__ = False  # 名字以 Test 开头，但不是测试类

    def __init__(
        self,
        policy: TargetPolicy,
        evaluator: AssertionEvaluator,
        credentials: CredentialProvider | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self.evaluator = evaluator
        self.credentials = credentials or CredentialProvider()
        self.environ = environ or {}

    def execute(
        self,
        case: TestCase,
        operation: Operation,
        provenance: RunProvenance,
        variables: dict[str, str] | None = None,
    ) -> TestRun:
        from .models import TerminationReason

        request_started = datetime.now(timezone.utc)
        run_id = uuid.uuid4().hex[:12]

        def denied_run(reason: str, detail: str, request_record: dict[str, Any]) -> TestRun:
            return TestRun(
                run_id=run_id,
                case_id=case.id,
                operation_id=case.operation_id,
                provenance=provenance,
                started_at=request_started,
                duration_ms=0,
                verdict=Verdict.INCONCLUSIVE,
                termination_reason=TerminationReason.POLICY_DENIED,
                request=request_record,
                observation=Observation(error=f"{reason}: {detail}"),
                assertion_results=[],
            )

        try:
            built = build_request(case, operation, provenance.target, variables)
        except PolicyDeniedError as exc:
            return denied_run(
                "请求构造被拒绝",
                str(exc),
                {"method": case.request.method, "path": case.request.path},
            )

        decision = self.policy.check_case(url=built.url, method=built.method, is_write=case.write)
        if not decision.allowed:
            return denied_run(
                "目标策略拒绝",
                decision.reason,
                {"method": built.method, "path": operation.path, "url": built.url},
            )

        headers = dict(built.headers)
        for name, value in self.credentials.headers(self.environ).items():
            headers.setdefault(name, value)

        request_record: dict[str, Any] = {
            "method": built.method,
            "path": operation.path,
            "url": built.url,
            "headers": sanitize_headers(headers),
            "query": sanitize_value(case.request.query or {}),
            "body": sanitize_value(built.json_body),
        }

        send_started = datetime.now(timezone.utc)
        observation, error = self._send(built, headers)
        if error is not None or observation is None:
            return TestRun(
                run_id=run_id,
                case_id=case.id,
                operation_id=case.operation_id,
                provenance=provenance,
                started_at=send_started,
                duration_ms=0,
                verdict=Verdict.INCONCLUSIVE,
                termination_reason=TerminationReason.REQUEST_FAILED,
                request=request_record,
                observation=Observation(error=error),
                assertion_results=[],
            )

        captured = _extract_captures(case, observation)
        results = self.evaluator.evaluate_all(case.assertions, observation, operation.operation_id)
        verdict, termination = decide_verdict(results)
        return TestRun(
            run_id=run_id,
            case_id=case.id,
            operation_id=case.operation_id,
            provenance=provenance,
            started_at=send_started,
            duration_ms=observation.duration_ms,
            verdict=verdict,
            termination_reason=termination,
            request=request_record,
            observation=observation,
            assertion_results=results,
            captures=captured,
        )

    def _send(self, built: BuiltRequest, headers: dict[str, str]) -> tuple[Observation | None, str | None]:
        attempts = max(1, self.policy.retries + 1)
        last_error: str | None = None
        for _ in range(attempts):
            began = time.perf_counter()
            try:
                with httpx.Client(
                    follow_redirects=False,
                    timeout=self.policy.timeout_ms / 1000,
                    # 允许范围必须是唯一的访问控制：不读取系统或环境代理，
                    # 否则请求会被静默转发到允许范围之外
                    trust_env=False,
                ) as client:
                    with client.stream(built.method, built.url, headers=headers, json=built.json_body) as response:
                        chunks: list[bytes] = []
                        total = 0
                        truncated = False
                        for chunk in response.iter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= self.policy.max_response_bytes:
                                truncated = True
                                break
                        raw = b"".join(chunks)[: self.policy.max_response_bytes]
                        status = response.status_code
                        response_headers = dict(response.headers)
                elapsed_ms = int((time.perf_counter() - began) * 1000)
                text = raw.decode("utf-8", errors="replace")
                body_json: Any | None = None
                json_error: str | None = None
                try:
                    body_json = json.loads(text)
                except json.JSONDecodeError as exc:
                    json_error = str(exc)
                if body_json is not None:
                    text = json.dumps(sanitize_value(body_json), ensure_ascii=False)
                else:
                    text = sanitize_text(text)
                return (
                    Observation(
                        status_code=status,
                        headers=sanitize_headers(response_headers),
                        body_text=text,
                        body_json=sanitize_value(body_json) if body_json is not None else None,
                        json_error=json_error,
                        duration_ms=elapsed_ms,
                        truncated=truncated,
                    ),
                    None,
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        return None, last_error


def _extract_captures(case: TestCase, observation: Observation) -> dict[str, str]:
    """按用例声明的路径从**脱敏后**的响应体里取值。取不到就没有，不编造。"""
    if not case.captures or observation.body_json is None:
        return {}
    captured: dict[str, str] = {}
    for name, path in case.captures.items():
        hit, value = get_json_path(observation.body_json, path)
        if hit and isinstance(value, (str, int, float, bool)):
            captured[name] = str(value)
    return captured


__all__ = [
    "BuiltRequest",
    "CredentialProvider",
    "TestRunner",
    "build_request",
    "probe_baseline",
    "resolve_captures",
]
