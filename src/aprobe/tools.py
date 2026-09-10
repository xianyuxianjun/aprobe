"""生成阶段暴露给 Agent 的工具注册表。

边界（ADR-0001 / ADR-0003）：
- 工具是与传输无关的注册表，只读内存中已加载的状态，**不发起任何网络请求**；
- `PlanContext` 就是它们的全部世界，Agent 拿不到目标地址；
- 参数按声明的 JSON Schema 校验，非法调用返回失败而不是抛异常；
- `submit_case` 与人类用的是**同一个校验器**（`cases.validate_cases`），
  所以 Agent 得到的是即时、确定性的反馈，而不是模型的自我评价。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .cases import operations_by_id, validate_cases
from .models import Specification, TestCase, TestRun


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolResult:
    ok: bool
    payload: dict[str, Any]
    summary: str
    error: str = ""


def history_from_runs(runs: list[TestRun]) -> dict[str, list[dict[str, str]]]:
    """把历史 Test Run 折成"某个 Operation 过去测得怎么样"。"""
    history: dict[str, list[dict[str, str]]] = {}
    for run in runs:
        history.setdefault(run.operation_id, []).append(
            {
                "case_id": run.case_id,
                "verdict": run.verdict.value,
                "termination_reason": run.termination_reason.value,
                "at": run.started_at.isoformat(),
            }
        )
    return history


class PlanContext:
    """一次生成过程能看到的全部状态。构造时加载完毕，运行期不再读盘。"""

    def __init__(
        self,
        specification: Specification,
        existing_cases: list[TestCase] | None = None,
        history: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self.specification = specification
        self.operations = operations_by_id(specification)
        self.existing_cases = list(existing_cases or [])
        self.history = history or {}
        self.submitted: list[TestCase] = []
        self.rejected: list[str] = []
        self._submitted_ids: set[str] = {case.id for case in self.existing_cases}

    # ---- 只读查询 ----

    def list_operations(self, tag: str | None = None, path_prefix: str | None = None) -> dict[str, Any]:
        items = []
        for operation in self.specification.operations:
            if tag and tag not in operation.tags:
                continue
            if path_prefix and not operation.path.startswith(path_prefix):
                continue
            items.append(
                {
                    "operation_id": operation.operation_id,
                    "method": operation.method,
                    "path": operation.path,
                    "summary": operation.summary,
                    "tags": operation.tags,
                    "has_required_input": self._needs_input(operation),
                    "declared_responses": [response.status for response in operation.responses],
                    "already_covered": any(
                        case.operation_id == operation.operation_id
                        for case in [*self.existing_cases, *self.submitted]
                    ),
                }
            )
        return {"operations": items, "total": len(items)}

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return operation_payload(self.specification, operation_id)

    def get_response_schema(self, operation_id: str, status: str) -> dict[str, Any]:
        """返回契约里声明的响应结构。Agent 不需要知道 JSON Pointer 的存在。"""
        return response_schema_payload(self.specification, operation_id, status)

    def list_existing_cases(self) -> dict[str, Any]:
        return {
            "cases": [
                {
                    "id": case.id,
                    "operation_id": case.operation_id,
                    "method": case.request.method,
                    "path": case.request.path,
                    "summary": case.summary,
                    "origin": case.origin,
                }
                for case in self.existing_cases
            ],
            "total": len(self.existing_cases),
        }

    def get_case_history(self, operation_id: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "runs": self.history.get(operation_id, [])}

    def covered_operations(self) -> set[str]:
        return {case.operation_id for case in [*self.existing_cases, *self.submitted]}

    def _needs_input(self, operation) -> bool:
        if operation.request_body_required:
            return True
        return any(parameter.required for parameter in operation.parameters)

    # ---- 唯一的写动作：提交用例（写进内存，由调用方落成文件） ----

    def submit_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            case = TestCase.model_validate(payload)
        except ValidationError as exc:
            problems = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
            self.rejected.append(f"{payload.get('id', '(no id)')}: 结构非法")
            return {"accepted": False, "problems": problems}
        if case.id in self._submitted_ids:
            return {"accepted": False, "problems": [f"用例 id 已存在: {case.id}"]}
        problems = validate_cases([case], self.specification)
        if problems:
            self.rejected.append(f"{case.id}: {'; '.join(problems)}")
            return {"accepted": False, "problems": problems}
        self.submitted.append(case)
        self._submitted_ids.add(case.id)
        return {"accepted": True, "case_id": case.id, "assertions": len(case.assertions)}


class ToolRegistry:
    """已注册工具的集合。输入输出都符合声明的结构，调用结果永远可序列化。"""

    def __init__(self, context: Any, specs: list[ToolSpec] | None = None) -> None:
        # context 可以是任何提供声明中用到的那些方法的对象；注册表本身与用途无关。
        # specs 缺省为生成用途的工具集，诊断等其它用途可以注入自己的集合。
        self.context = context
        source = specs if specs is not None else _build_specs(context)
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in source}

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    def tool_declarations(self) -> list[dict[str, Any]]:
        """给模型的工具声明（OpenAI-compatible 形状）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in (self._specs[name] for name in self.names)
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(ok=False, payload={}, summary=f"未注册的工具 {name}", error="unknown_tool")
        errors = sorted(
            Draft202012Validator(spec.parameters).iter_errors(arguments), key=lambda item: list(item.path)
        )
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.path) or "(root)"
            return ToolResult(
                ok=False,
                payload={},
                summary=f"{name} 参数非法：{location}",
                error=first.message,
            )
        try:
            payload = spec.handler(arguments)
        except Exception as exc:  # 工具失败是事实，不是异常：记录下来让 Agent 继续
            return ToolResult(
                ok=False, payload={}, summary=f"{name} 执行失败", error=f"{type(exc).__name__}: {exc}"
            )
        summary = _summarize(name, payload)
        # 工具用它自己的返回值宣告失败（error 字段，或 submit_case 的 accepted=false），
        # ok 只在这里决定一次，避免出现“调用成功但结果是被拒绝”这种自相矛盾的轨迹
        if "error" in payload or payload.get("accepted") is False:
            return ToolResult(
                ok=False,
                payload=payload,
                summary=summary,
                error=str(payload.get("error") or "被拒绝"),
            )
        return ToolResult(ok=True, payload=payload, summary=summary)


def _summarize(name: str, payload: dict[str, Any]) -> str:
    if name == "list_operations":
        return f"返回 {payload.get('total', 0)} 个 Operation"
    if name == "get_operation":
        return f"{payload.get('method')} {payload.get('path')}"
    if name == "get_response_schema":
        return f"{payload.get('operation_id')} 的 {payload.get('status')} 响应结构"
    if name == "list_existing_cases":
        return f"已有 {payload.get('total', 0)} 条用例"
    if name == "get_case_history":
        return f"{len(payload.get('runs', []))} 条历史运行"
    if name == "submit_case":
        if payload.get("accepted"):
            return f"已接受 {payload.get('case_id')}"
        return "被拒绝：" + "; ".join(str(item) for item in payload.get("problems", []))
    return "ok"


def _build_specs(context: PlanContext) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_operations",
            description="列出规范中的 Operation 摘要，可按 tag 或路径前缀过滤。返回摘要，不含完整结构。",
            parameters={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "path_prefix": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=lambda args: context.list_operations(args.get("tag"), args.get("path_prefix")),
        ),
        *spec_tool_specs(context),
        ToolSpec(
            name="list_existing_cases",
            description="列出用例文件中已有的用例，用于避免重复并保持风格一致。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args: context.list_existing_cases(),
        ),
        ToolSpec(
            name="get_case_history",
            description="取某个 Operation 过去的运行结论，用于聚焦从未覆盖或曾经失败的接口。",
            parameters={
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            handler=lambda args: context.get_case_history(args["operation_id"]),
        ),
        ToolSpec(
            name="submit_case",
            description=(
                "提交一条测试用例。结构与内容会被确定性校验：Operation 必须存在、路径必须与规范一致、"
                "至少一条断言、响应码必须已声明、写方法必须声明 write=true。被拒绝时返回具体问题，可修正后重提。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "小写字母、数字、-_.，不超过 64 字符"},
                    "operation_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "request": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string"},
                            "path": {"type": "string"},
                            "path_values": {"type": "object", "additionalProperties": {"type": "string"}},
                            "query": {"type": "object"},
                            "headers": {"type": "object"},
                            "body": {},
                        },
                        "required": ["method", "path"],
                        "additionalProperties": False,
                    },
                    "assertions": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {"kind": {"type": "string"}},
                            "required": ["kind"],
                        },
                    },
                    "requires": {"type": "array", "items": {"type": "string"}},
                    "write": {"type": "boolean"},
                },
                "required": ["id", "operation_id", "request", "assertions"],
                "additionalProperties": False,
            },
            handler=lambda args: context.submit_case({**args, "origin": "agent"}),
        ),
    ]


# ---- 只依赖规范的只读工具：生成与诊断两种用途共用，边界因此只有一份 ----

def operation_payload(specification: Specification, operation_id: str) -> dict[str, Any]:
    operation = operations_by_id(specification).get(operation_id)
    if operation is None:
        return {"error": f"规范中不存在 operation_id={operation_id}"}
    return {
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path,
        "summary": operation.summary,
        "security": operation.security,
        "parameters": [
            {
                "name": parameter.name,
                "in": parameter.location,
                "required": parameter.required,
                "schema": parameter.schema_,
            }
            for parameter in operation.parameters
        ],
        "request_body_required": operation.request_body_required,
        "request_body_media_type": operation.request_body_media_type,
        "responses": [
            {
                "status": response.status,
                "description": response.description,
                "media_type": response.media_type,
                "has_json_schema": bool(response.schema_pointer),
            }
            for response in operation.responses
        ],
    }


def response_schema_payload(specification: Specification, operation_id: str, status: str) -> dict[str, Any]:
    try:
        schema = specification.response_schema(operation_id, status)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"operation_id": operation_id, "status": status, "schema": schema}


def spec_tool_specs(queries: Any) -> list[ToolSpec]:
    """`queries` 只需提供 get_operation / get_response_schema 两个方法。"""
    return [
        ToolSpec(
            name="get_operation",
            description="取一个 Operation 的完整契约：参数、请求体、已声明的响应与鉴权。",
            parameters={
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            handler=lambda args: queries.get_operation(args["operation_id"]),
        ),
        ToolSpec(
            name="get_response_schema",
            description="取某个 Operation 某个响应码在契约中声明的响应结构。",
            parameters={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "status": {"type": "string", "description": "响应码，例如 200 或 default"},
                },
                "required": ["operation_id", "status"],
                "additionalProperties": False,
            },
            handler=lambda args: queries.get_response_schema(args["operation_id"], args["status"]),
        ),
    ]
