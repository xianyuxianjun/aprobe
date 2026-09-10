"""有界多步 Agent 循环（生成阶段）。

边界：
- Agent 的自主权只覆盖「查什么、按什么顺序、提交什么用例、何时停」；
- 它没有通往被测目标的工具，也拿不到目标地址（ADR-0001）；
- 步数 / token / 时长三条预算耗尽即显式终止，且不是通过也不是失败；
- 权威轨迹由本模块自己累积在 `self._steps`，**不放在 LangGraph 的 state 里**（ADR-0002）。

图刻意保持很薄：两个节点（decide / act）加一条条件边。它负责流转，
不负责事实。
"""

from __future__ import annotations

import time
import uuid
import json
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .models import (
    AgentBudget,
    AgentRun,
    AgentStep,
    TerminationReason,
    ToolCallRecord,
)
from .model_client import ModelClient, ModelError
from .tools import ToolRegistry

class _State(TypedDict, total=False):
    messages: list[dict[str, Any]]
    step_index: int
    failed: str


class LoopOutcome:
    def __init__(self, agent_run: AgentRun) -> None:
        self.agent_run = agent_run


class AgentLoop:
    """把一个 ToolRegistry 和一个 ModelClient 放进一个有界循环。"""

    def __init__(
        self,
        registry: ToolRegistry,
        model: ModelClient,
        *,
        system_prompt: str,
        initial_message: str,
        budget: AgentBudget | None = None,
        prompt_version: str = "loop-0.0.1",
        mode: str = "agent",
    ) -> None:
        self.registry = registry
        self.model = model
        self.system_prompt = system_prompt
        self.initial_message = initial_message
        self.budget = budget or AgentBudget()
        self.prompt_version = prompt_version
        self.mode = mode
        self._steps: list[AgentStep] = []
        self._notes: list[str] = []
        self._started = 0.0

    # ---- 图的两个节点 ----

    def _decide(self, state: _State) -> _State:
        step = AgentStep(
            index=state.get("step_index", 0),
            started_at=_utcnow(),
            model=self.model.name,
            prompt_version=self.prompt_version,
        )
        began = time.perf_counter()
        try:
            reply = self.model.complete(
                system=self.system_prompt,
                messages=state["messages"],
                tools=self.registry.tool_declarations(),
            )
        except ModelError as exc:
            step.duration_ms = int((time.perf_counter() - began) * 1000)
            self._steps.append(step)
            self._notes.append(f"模型调用失败：{exc}")
            return {"failed": str(exc), "step_index": step.index + 1, "messages": state["messages"]}

        step.duration_ms = int((time.perf_counter() - began) * 1000)
        step.text = reply.text[:2000]
        step.input_tokens = reply.input_tokens
        step.output_tokens = reply.output_tokens
        step.tool_calls = [
            ToolCallRecord(name=call.name, arguments=call.arguments, ok=False, error=call.parse_error)
            for call in reply.tool_calls
        ]
        self._steps.append(step)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": reply.text}
        if reply.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                }
                for call in reply.tool_calls
            ]
        return {
            "messages": [*state["messages"], assistant_message],
            "step_index": step.index + 1,
        }

    def _act(self, state: _State) -> _State:
        step = self._steps[-1]
        messages = list(state["messages"])
        pending = _last_assistant_tool_calls(messages)
        for index, (call_id, name, arguments) in enumerate(pending):
            began = time.perf_counter()
            result = self.registry.call(name, arguments)
            record = step.tool_calls[index] if index < len(step.tool_calls) else ToolCallRecord(name=name, ok=result.ok)
            record.name = name
            record.arguments = arguments
            record.ok = result.ok
            record.duration_ms = int((time.perf_counter() - began) * 1000)
            record.result_summary = result.summary
            record.error = record.error or result.error
            if index < len(step.tool_calls):
                step.tool_calls[index] = record
            else:
                step.tool_calls.append(record)
            body: dict[str, Any] = {"ok": result.ok, "result": result.payload}
            if result.error:
                body["error"] = result.error
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(body, ensure_ascii=False, default=str)[:4000],
                }
            )
        return {"messages": messages}

    # ---- 循环控制 ----

    def _should_continue(self, state: _State) -> str:
        if state.get("failed"):
            return "stop"
        if not _last_assistant_tool_calls(state["messages"]):
            # 模型没有再要求调工具，就是它认为可以停了
            return "stop"
        if self.budget_exhausted() is not None:
            return "stop"
        return "decide"

    def budget_exhausted(self) -> str | None:
        elapsed_ms = int((time.perf_counter() - self._started) * 1000)
        consumed_tokens = sum(step.input_tokens + step.output_tokens for step in self._steps)
        if len(self._steps) >= self.budget.max_steps:
            return f"达到步数上限 {self.budget.max_steps}"
        if consumed_tokens >= self.budget.max_tokens:
            return f"达到 token 上限 {self.budget.max_tokens}"
        if elapsed_ms >= self.budget.max_ms:
            return f"达到时长上限 {self.budget.max_ms}ms"
        return None

    def run(self) -> LoopOutcome:
        self._started = time.perf_counter()
        graph = StateGraph(_State)
        graph.add_node("decide", self._decide)
        graph.add_node("act", self._act)
        graph.set_entry_point("decide")
        graph.add_edge("decide", "act")
        graph.add_conditional_edges("act", self._should_continue, {"decide": "decide", "stop": END})
        app = graph.compile()

        # 图的递归上限只是兜底；真正的预算是我们自己算的那三条
        app.invoke(
            {"messages": [{"role": "user", "content": self.initial_message}], "step_index": 0},
            config={"recursion_limit": 2 * self.budget.max_steps + 5},
        )

        exhausted = self.budget_exhausted()
        failed = self._notes[-1] if self._notes else ""
        if failed:
            reason = TerminationReason.PLANNER_FAILED
        elif exhausted:
            reason = TerminationReason.BUDGET_EXHAUSTED
            self._notes.append(exhausted)
        else:
            reason = TerminationReason.COMPLETED

        # 循环只知道“跑了什么”，不知道“产出算不算成果”——那是调用方的意图，不是机制
        agent_run = AgentRun(
            run_id=uuid.uuid4().hex[:12],
            mode=self.mode,
            model=self.model.name,
            prompt_version=self.prompt_version,
            budget=self.budget,
            termination_reason=reason,
            steps=self._steps,
            notes=list(self._notes),
        )
        return LoopOutcome(agent_run)


def _last_assistant_tool_calls(messages: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """取最近一条 assistant 消息里的工具调用。

    必须往回找 assistant，而不是看最后一条消息：`act` 会把工具结果以 tool 消息
    追加进去，直接看最后一条会在第一步之后就误判为“可以停了”。
    """
    last_assistant = next((item for item in reversed(messages) if item.get("role") == "assistant"), None)
    if last_assistant is None:
        return []
    pending: list[tuple[str, str, dict[str, Any]]] = []
    for raw in last_assistant.get("tool_calls") or []:
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        arguments: dict[str, Any] = {}
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
                arguments = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                arguments = {}
        pending.append((str(raw.get("id") or ""), str(function.get("name") or ""), arguments))
    return pending


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
