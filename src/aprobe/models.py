"""aprobe 的领域模型。

这里只放领域概念本身，不放行为：判定逻辑在 assertions，网络在 runner，
策略在 policy，持久化在 trace。术语以本地开发文档 CONTEXT.md 为准（该文件不随仓库发布）。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

APROBE_VERSION = "0.0.1"
DETERMINISTIC_PLANNER_VERSION = "deterministic-0.0.1"
AGENT_PLANNER_VERSION = "agent-0.0.1"


class Verdict(str, enum.Enum):
    """Test Run 的结论。无法判定不是通过。"""

    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class TerminationReason(str, enum.Enum):
    """一次 Test Run 为什么停下。它不是结论，只是原因。"""

    COMPLETED = "completed"
    POLICY_DENIED = "policy_denied"
    REQUEST_FAILED = "request_failed"
    UNEVALUABLE = "unevaluable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PLANNER_FAILED = "planner_failed"


class AssertionKind(str, enum.Enum):
    STATUS = "status"
    JSON_SCHEMA = "json_schema"
    JSON_PATH = "json_path"
    HEADER = "header"
    RESPONSE_TIME_MS = "response_time_ms"


#: 每种 Assertion 允许出现的字段，用来拒绝"看起来像断言、其实没人求值"的写法。
_ASSERTION_FIELDS: dict[AssertionKind, frozenset[str]] = {
    AssertionKind.STATUS: frozenset({"in"}),
    AssertionKind.JSON_SCHEMA: frozenset({"response", "schema"}),
    AssertionKind.JSON_PATH: frozenset(
        {
            "path",
            "equals",
            "equals_path",
            "length_equals_path",
            "exists",
            "type",
            "contains",
            "length_equals",
            "min_length",
            "max_length",
        }
    ),
    AssertionKind.HEADER: frozenset({"name", "equals", "contains", "exists"}),
    AssertionKind.RESPONSE_TIME_MS: frozenset({"max"}),
}


class Assertion(BaseModel):
    """一条声明式判定条件。它不含可执行代码，由 assertions 模块确定性求值。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: AssertionKind

    # status
    in_: list[int] | None = Field(default=None, alias="in")

    # json_schema：response 是响应码（指向契约里该 Operation 已声明的响应），schema 为内联 JSON Schema
    response: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    # json_path / header
    name: str | None = None
    path: str | None = None
    equals: Any | None = None
    # 跟同一响应里的另一个字段比较：equals_path 比本身，length_equals_path 比长度
    equals_path: str | None = None
    length_equals_path: str | None = None
    exists: bool | None = None
    type: str | None = None
    contains: Any | None = None
    length_equals: int | None = None
    min_length: int | None = None
    max_length: int | None = None

    # response_time_ms
    max: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "Assertion":
        allowed = _ASSERTION_FIELDS[self.kind]
        present = {
            field
            for field in self.model_fields_set
            if field != "kind" and getattr(self, field, None) is not None
        }
        fields = type(self).model_fields
        # 统一用对外别名（in / schema）做字段判断，避免 in_ 与 schema_ 这类内部名泄到报错信息里
        normalised = {fields[field].alias or field for field in present}
        unknown = normalised - allowed
        if unknown:
            raise ValueError(f"{self.kind.value} 断言不支持字段: {sorted(unknown)}")
        if not normalised:
            raise ValueError(f"{self.kind.value} 断言至少需要一个字段")

        if self.kind is AssertionKind.JSON_SCHEMA:
            if (self.response is None) == (self.schema_ is None):
                raise ValueError("json_schema 断言必须且只能提供 response 或 schema 之一")
        if self.kind is AssertionKind.JSON_PATH:
            if self.path is None:
                raise ValueError("json_path 断言必须提供 path")
            operators = {
                "equals",
                "equals_path",
                "length_equals_path",
                "exists",
                "type",
                "contains",
                "length_equals",
                "min_length",
                "max_length",
            }
            if not (normalised & operators):
                raise ValueError("json_path 断言必须至少提供一个求值算子，否则只能得到无法判定")
        if self.kind is AssertionKind.HEADER:
            if self.name is None:
                raise ValueError("header 断言必须提供 name")
            if not (normalised & {"equals", "contains", "exists"}):
                raise ValueError("header 断言必须至少提供一个求值算子")
        return self


class ParameterDecl(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


class ResponseDecl(BaseModel):
    status: str
    description: str = ""
    media_type: str | None = None
    schema_pointer: str | None = None


class Operation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    operation_id: str
    method: str
    path: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[ParameterDecl] = Field(default_factory=list)
    request_body_required: bool = False
    request_body_media_type: str | None = None
    responses: list[ResponseDecl] = Field(default_factory=list)
    security: list[str] = Field(default_factory=list)
    pointer: str = ""


class Specification(BaseModel):
    """一份已解析的 OpenAPI 规范。resolve() 是 schema 引用的唯一解析入口。"""

    document: dict[str, Any]
    openapi_version: str
    title: str
    version: str
    source: str
    operations: list[Operation] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)

    def resolve(self, pointer: str) -> Any:
        """按 JSON Pointer 解析节点，并沿本地 $ref 链解析到底。"""
        from .specification import resolve_pointer

        node = resolve_pointer(self.document, pointer)
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#"):
                raise ValueError(f"只允许本地 $ref，收到: {ref!r}")
            if ref in seen:
                raise ValueError(f"循环引用: {ref}")
            if len(seen) > 32:
                raise ValueError("$ref 嵌套过深")
            seen.add(ref)
            node = resolve_pointer(self.document, ref)
        return node

    def operation(self, operation_id: str) -> Operation:
        for candidate in self.operations:
            if candidate.operation_id == operation_id:
                return candidate
        raise ValueError(f"规范中不存在 operation_id={operation_id}")

    def response_schema(self, operation_id: str, status: str) -> dict[str, Any]:
        """某个 Operation 某个响应码所声明的 JSON schema，已解析为自包含文档。

        用例只声明“哪个 Operation 的哪个响应”，JSON Pointer 是本模块的实现细节，
        不允许出现在用例文件里。
        """
        operation = self.operation(operation_id)
        for response in operation.responses:
            if response.status == str(status):
                if not response.schema_pointer:
                    raise ValueError(f"{operation_id} 的 {status} 响应未声明 JSON schema")
                return self.schema_document(response.schema_pointer)
        declared = [item.status for item in operation.responses]
        raise ValueError(f"{operation_id} 未声明 {status} 响应（已声明：{declared}）")

    def schema_document(self, pointer: str) -> dict[str, Any]:
        """取出一份可以直接交给 JSON Schema 校验器的自包含文档。

        嵌套的 `#/components/schemas/X` 会被重写为 `#/$defs/X` 并一并打包，
        这样递归 schema 也能被校验器惰性解析，不需要我们自己做深拷贝展开。
        """
        from .specification import bundle_local_refs

        node = self.resolve(pointer)
        if not isinstance(node, dict):
            raise ValueError(f"Pointer 指向的不是 schema 对象: {pointer}")
        return bundle_local_refs(node, self.document)


class RunProvenance(BaseModel):
    """一次运行“谁在什么时候测了什么”的全部事实。

    报告不另外接收这些参数，而是从 Trace 里读它们（ADR-0002），
    这样报告不可能与 Trace 不一致。
    """

    target: str = ""
    spec_source: str = ""
    spec_title: str = ""
    spec_version: str = ""
    cases_file: str = ""
    aprobe_version: str = APROBE_VERSION
    planner: str = DETERMINISTIC_PLANNER_VERSION


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    path_values: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None


class TestCase(BaseModel):
    """一条可人工审阅的测试意图。它是意图，不是执行事实。"""

    model_config = ConfigDict(extra="forbid")

    __test__ = False  # 名字以 Test 开头，但不是测试类

    id: str
    operation_id: str
    summary: str = ""
    request: RequestSpec
    assertions: list[Assertion] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    write: bool = False
    origin: Literal["deterministic", "agent", "human"] = "deterministic"


class Observation(BaseModel):
    """一次请求观察到的结果。已经过脱敏，可以落库。"""

    status_code: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_text: str | None = None
    body_json: Any | None = None
    json_error: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    error: str | None = None


class AssertionResult(BaseModel):
    assertion: Assertion
    verdict: Verdict
    observed: str
    detail: str = ""


class TestRun(BaseModel):
    """Test Case 的一次实际执行。可回放，且是执行事实。"""

    __test__ = False  # 名字以 Test 开头，但不是测试类

    run_id: str
    case_id: str
    operation_id: str
    started_at: datetime
    duration_ms: int
    verdict: Verdict
    termination_reason: TerminationReason
    request: dict[str, Any] = Field(default_factory=dict)
    observation: Observation = Field(default_factory=Observation)
    assertion_results: list[AssertionResult] = Field(default_factory=list)
    provenance: RunProvenance = Field(default_factory=RunProvenance)


class FailureCategory(str, enum.Enum):
    """一次失败归因的类别。它是模型建议，不是 Verdict。"""

    INTERFACE_DEFECT = "interface_defect"
    CASE_DEFECT = "case_defect"
    ENVIRONMENT = "environment"
    INCONCLUSIVE = "inconclusive"


class FailureAttribution(BaseModel):
    """对一条失败 Test Run 的归因。

    边界：它不修改 TestRun 的 Verdict，也不会去改用例文件。它只是对“为什么会失败”
    的一个带证据的提议，供人判断。
    """

    run_id: str
    case_id: str
    operation_id: str
    category: FailureCategory
    reason: str
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    model: str = ""
    prompt_version: str = ""
    agent_run_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolCallRecord(BaseModel):
    """Agent 对某个已注册工具的一次调用。它是执行事实，不是模型自述。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    result_summary: str = ""
    duration_ms: int = 0
    error: str = ""


class AgentStep(BaseModel):
    """Agent 循环中的一次迭代：一次模型请求 + 它产生的工具调用。"""

    index: int
    started_at: datetime
    duration_ms: int = 0
    model: str = ""
    prompt_version: str = ""
    text: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class AgentBudget(BaseModel):
    """一次 Agent 循环允许消耗的上限。耗尽不是通过，也不是失败。"""

    max_steps: int = 8
    max_tokens: int = 24000
    max_ms: int = 120_000


class AgentRun(BaseModel):
    """Agent 循环的权威轨迹。与 TestRun 分开保存：它描述“怎么想出来的”，不是“测出了什么”。"""

    run_id: str
    mode: Literal["degraded", "agent", "diagnose"]
    model: str = ""
    prompt_version: str = ""
    budget: AgentBudget = Field(default_factory=AgentBudget)
    termination_reason: TerminationReason
    steps: list[AgentStep] = Field(default_factory=list)
    produced_case_ids: list[str] = Field(default_factory=list)
    needs_input: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def consumed_steps(self) -> int:
        return len(self.steps)

    @property
    def consumed_tokens(self) -> int:
        return sum(step.input_tokens + step.output_tokens for step in self.steps)

    @property
    def consumed_ms(self) -> int:
        return sum(step.duration_ms for step in self.steps)

    def budget_exhausted(self, elapsed_ms: int) -> str | None:
        """返回耗尽的原因；未耗尽时返回 None。"""
        if self.consumed_steps >= self.budget.max_steps:
            return f"达到步数上限 {self.budget.max_steps}"
        if self.consumed_tokens >= self.budget.max_tokens:
            return f"达到 token 上限 {self.budget.max_tokens}"
        if elapsed_ms >= self.budget.max_ms:
            return f"达到时长上限 {self.budget.max_ms}ms"
        return None
