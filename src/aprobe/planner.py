"""规划器 seam：把一份规范变成一组可审阅的 Test Case。

两个 adapter（ADR-0003）：
- **降级模式**：`generator` 的确定性生成，不调用任何模型；
- **Agent 模式**：`agent_loop` 的有界循环，用注册工具查规范、写用例。

两者的产物形状完全相同（TestCase 列表 + needs_input 列表），因此执行、判定、
报告与指标都不需要知道用了哪一种——这正是两种模式可比的前提。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .errors import ConfigError
from .generator import GenerationResult, NeedsInput, generate_cases
from .models import (
    AGENT_PLANNER_VERSION,
    DETERMINISTIC_PLANNER_VERSION,
    AgentBudget,
    AgentRun,
    Specification,
    TerminationReason,
    TestCase,
)
from .tools import PlanContext, ToolRegistry

MODES = ("degraded", "agent", "auto")

GENERATE_PROMPT_VERSION = "generate-0.0.1"

GENERATE_SYSTEM_PROMPT = """你是接口契约测试的用例设计者。你的产出是一份可被人审阅的测试用例集合。

你能做的事只有三件：查询 OpenAPI 规范、查看已有用例与历史结论、提交测试用例。
你不能发起任何网络请求，也不能访问被测服务——你只能依据规范写用例。

必须遵守：
1. 只能引用规范中真实存在的 operation_id、路径与响应码，不得编造。
2. 用例只声明“哪个 Operation 的哪个响应”（json_schema 断言用 response 字段），
   不要写 JSON Pointer 或任何规范内部结构。
3. 每条用例至少有一条断言。断言是确定性的：status / json_schema / json_path / header / response_time_ms。
   其中 json_path 支持 equals、equals_path、length_equals_path、exists、type、contains、
   length_equals、min_length、max_length。
4. 需要路径参数或请求体才能调用的 Operation，如果你无法从规范确定合法取值，就不要为它造用例——
   留空比编造一个假 id 更有价值。
5. 优先写能表达业务规则的断言（例如 total 必须等于 items 的条数），而不只是状态码。
6. 提交被拒绝时，按返回的具体问题修正后重试；同一错误不要重复提交。
7. 覆盖完之后，直接给出停止理由，不要再调用工具。

最后一条回复不要带工具调用，用一句话说明你覆盖了什么、哪些你没有覆盖以及为什么。"""


def build_generation_loop(registry: ToolRegistry, model, budget: AgentBudget | None = None):
    """构造生成用途的循环：机制在 agent_loop，意图（提示词与开场白）在这里。"""
    from .agent_loop import AgentLoop

    return AgentLoop(
        registry,
        model,
        system_prompt=GENERATE_SYSTEM_PROMPT,
        initial_message=_initial_message(registry.context),
        budget=budget,
        prompt_version=GENERATE_PROMPT_VERSION,
        mode="agent",
    )


def _initial_message(context: PlanContext) -> str:
    covered = sorted(context.covered_operations())
    return (
        f"这是一份 OpenAPI 规范（{context.specification.title} {context.specification.version}），"
        f"共 {len(context.specification.operations)} 个 Operation。"
        f"用例文件中已有 {len(context.existing_cases)} 条用例，覆盖 {len(covered)} 个 Operation。\n"
        "请先了解规范，再提交你为尚未覆盖的 Operation 设计的测试用例。"
    )


@dataclass
class PlanResult:
    cases: list[TestCase] = field(default_factory=list)
    needs_input: list[NeedsInput] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    agent_run: AgentRun | None = None


def planner_label(cases: list[TestCase]) -> str:
    """这次运行执行的是谁设计的用例。指标要按它分组才有意义。"""
    origins = {case.origin for case in cases}
    if not origins:
        return "none"
    if len(origins) == 1:
        return {
            "deterministic": DETERMINISTIC_PLANNER_VERSION,
            "agent": AGENT_PLANNER_VERSION,
            "human": "human",
        }[origins.pop()]
    return "mixed"


def plan(
    specification: Specification,
    *,
    mode: str = "degraded",
    existing_cases: list[TestCase] | None = None,
    history: dict[str, list[dict[str, str]]] | None = None,
    budget: AgentBudget | None = None,
    model=None,
) -> PlanResult:
    if mode not in MODES:
        raise ConfigError(f"未知的规划模式: {mode}（可选：{', '.join(MODES)}）")
    existing = list(existing_cases or [])
    resolved = ("agent" if model is not None else "degraded") if mode == "auto" else mode

    if resolved == "degraded":
        return _plan_degraded(specification, existing, budget, requested=mode)
    if model is None:
        raise ConfigError("agent 模式需要模型端点：设置 APROBE_MODEL_BASE_URL 与 APROBE_MODEL")
    return _plan_agent(specification, existing, history, budget, model)


def _plan_degraded(
    specification: Specification,
    existing: list[TestCase],
    budget: AgentBudget | None,
    *,
    requested: str,
) -> PlanResult:
    generated: GenerationResult = generate_cases(specification)
    cases, dropped = _merge(existing, generated.cases)
    notes = ["降级模式：未使用模型，用例由确定性规则从规范推导"]
    if requested == "auto":
        notes.append("auto：未检测到模型端点，退回降级模式")
    if dropped:
        notes.append(f"已有用例包含同 id 用例，保留已有版本：{', '.join(dropped)}")
    return PlanResult(
        cases=cases,
        needs_input=generated.needs_input,
        ignored=generated.ignored,
        agent_run=AgentRun(
            run_id=uuid.uuid4().hex[:12],
            mode="degraded",
            budget=budget or AgentBudget(),
            termination_reason=TerminationReason.COMPLETED,
            produced_case_ids=[case.id for case in generated.cases],
            needs_input=[item.operation_id for item in generated.needs_input],
            notes=notes,
        ),
    )


def _plan_agent(
    specification: Specification,
    existing: list[TestCase],
    history: dict[str, list[dict[str, str]]] | None,
    budget: AgentBudget | None,
    model,
) -> PlanResult:
    try:
        from .agent_loop import AgentLoop  # noqa: F401
    except ImportError as exc:  # langgraph 是可选依赖
        raise ConfigError("agent 模式需要可选依赖：uv pip install -e '.[agent]'") from exc

    context = PlanContext(specification, existing_cases=existing, history=history)
    outcome = build_generation_loop(ToolRegistry(context), model, budget).run()
    cases, dropped = _merge(existing, context.submitted)
    agent_run = outcome.agent_run
    agent_run.produced_case_ids = [case.id for case in context.submitted]
    agent_run.notes.extend(context.rejected[:10])
    if dropped:
        agent_run.notes.append(f"与已有用例同 id，已丢弃：{', '.join(dropped)}")
    uncovered = _uncovered(specification, cases)
    agent_run.needs_input = [item.operation_id for item in uncovered]
    return PlanResult(
        cases=cases,
        needs_input=uncovered,
        ignored=list(specification.ignored),
        agent_run=agent_run,
    )


def _merge(existing: list[TestCase], produced: list[TestCase]) -> tuple[list[TestCase], list[str]]:
    """已有用例优先：它们已经被人审阅过，不能被新产物悄悄替换（ADR-0001）。"""
    taken = {case.id for case in existing}
    dropped: list[str] = []
    merged = list(existing)
    for case in produced:
        if case.id in taken:
            dropped.append(case.id)
            continue
        taken.add(case.id)
        merged.append(case)
    return merged, dropped


def _uncovered(specification: Specification, cases: list[TestCase]) -> list[NeedsInput]:
    """未覆盖的 Operation 由确定性代码算出，不问模型。"""
    covered = {case.operation_id for case in cases}
    uncovered: list[NeedsInput] = []
    for operation in specification.operations:
        if operation.operation_id in covered:
            continue
        if operation.request_body_required:
            reason = "需要请求体，未生成用例"
        elif any(parameter.required for parameter in operation.parameters):
            reason = "需要路径或查询参数值，未生成用例"
        else:
            reason = "尚未为该 Operation 生成用例"
        uncovered.append(NeedsInput(operation.operation_id, operation.method, operation.path, reason))
    return uncovered
