"""失败归因（诊断阶段）。

边界：
- **只读**：能读 TestRun、用例、规范与同 Operation 的历史运行；不能改用例文件，不能发请求；
- 归因是**模型建议**，不是 Verdict：它不修改任何已有结论，只供人判断；
- **归因必须有证据**：没有 evidence 的提交会被确定性拒绝（这条规则由代码强制，不靠提示词）；
- 必须区分"接口缺陷 / 用例缺陷 / 环境问题 / 无法判定"，不能只给一句现象描述。

诊断没有降级模式：归因是判断，不是计算，不存在确定性的替代实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import ConfigError
from .models import (
    AgentBudget,
    AgentRun,
    FailureAttribution,
    FailureCategory,
    Specification,
    TestCase,
    TestRun,
    Verdict,
)
from .tools import PlanContext, ToolRegistry, ToolSpec, spec_tool_specs

DIAGNOSE_PROMPT_VERSION = "diagnose-0.0.1"

DIAGNOSE_SYSTEM_PROMPT = """你是接口契约测试的失败归因者。一条测试已经跑完并给出了结论，你要判断问题出在哪里。

你能做的事只有四件：读这次运行的完整事实、读该用例的定义、读接口契约、读同一接口的历史运行，
然后提交一条归因。你不能发请求，也不能修改用例。

归因只有四个类别，必须选一个：
- interface_defect：契约与实现不一致，或实现违反了契约（接口的问题）
- case_defect：用例本身写错了（路径参数取值错、断言与契约不符、前提不成立）
- environment：请求没发出、超时、连接失败、凭据缺失等环境问题
- inconclusive：证据不足以判断

必须遵守：
1. **每条归因都必须给出 evidence**，指向你实际看到的事实（例如某条断言的观察值、状态码、契约里的字段类型）。
   没有证据的归因会被确定性代码拒绝。
2. 只能选一个类别，不要写"可能是 A 也可能是 B"。分不清就选 inconclusive，并说明缺少什么证据。
3. 不要复述现象，要给出判断依据：为什么这些事实支持你选的类别。
4. 不要建议修改被测系统或自动修复，suggested_fix 只描述方向（例如"确认契约中 total 的类型"）。
5. 提交之后用一句话说明结论，不要再调用工具。"""


class DiagnosisContext:
    """一次归因能看到的全部状态。构造时加载完毕，运行期不再读盘。"""

    def __init__(
        self,
        specification: Specification,
        run: TestRun,
        case: TestCase | None = None,
        related_runs: list[TestRun] | None = None,
    ) -> None:
        self.specification = specification
        self.run = run
        self.case = case
        self.related_runs = list(related_runs or [])
        self.attribution: FailureAttribution | None = None
        self.rejected: list[str] = []
        self._queries = PlanContext(specification)

    # ---- 只读查询 ----

    def get_run(self) -> dict[str, Any]:
        observation = self.run.observation
        return {
            "run_id": self.run.run_id,
            "case_id": self.run.case_id,
            "operation_id": self.run.operation_id,
            "verdict": self.run.verdict.value,
            "termination_reason": self.run.termination_reason.value,
            "duration_ms": self.run.duration_ms,
            "request": self.run.request,
            "status_code": observation.status_code,
            "response_body": (observation.body_text or "")[:2000],
            "response_truncated": observation.truncated,
            "request_error": observation.error,
            "assertions": [
                {
                    "kind": result.assertion.kind.value,
                    "verdict": result.verdict.value,
                    "observed": result.observed,
                    "detail": result.detail,
                }
                for result in self.run.assertion_results
            ],
        }

    def get_case(self) -> dict[str, Any]:
        if self.case is None:
            return {"error": f"用例 {self.run.case_id} 不在当前用例文件中（可能已被删除或重命名）"}
        return self.case.model_dump(mode="json", exclude_none=True, by_alias=True)

    def list_related_runs(self) -> dict[str, Any]:
        return {
            "operation_id": self.run.operation_id,
            "runs": [
                {
                    "run_id": other.run_id,
                    "case_id": other.case_id,
                    "verdict": other.verdict.value,
                    "termination_reason": other.termination_reason.value,
                    "status_code": other.observation.status_code,
                    "at": other.started_at.isoformat(),
                }
                for other in self.related_runs[:10]
            ],
        }

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._queries.get_operation(operation_id)

    def get_response_schema(self, operation_id: str, status: str) -> dict[str, Any]:
        return self._queries.get_response_schema(operation_id, status)

    # ---- 唯一的写动作：提交归因（写进内存，由调用方落库） ----

    def submit_diagnosis(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_category = str(payload.get("category") or "")
        try:
            category = FailureCategory(raw_category)
        except ValueError:
            return {
                "accepted": False,
                "problems": [
                    f"未知类别 {raw_category!r}",
                    f"可选：{[item.value for item in FailureCategory]}",
                ],
            }
        reason = str(payload.get("reason") or "").strip()
        evidence = [str(item).strip() for item in (payload.get("evidence") or []) if str(item).strip()]
        problems: list[str] = []
        if not reason:
            problems.append("reason 不能为空：需要说明为什么这些事实支持该类别")
        if not evidence:
            # 这条规则由代码强制：「无依据结论」在归因里同样不允许
            problems.append("evidence 不能为空：归因必须指向具体观察事实")
        if problems:
            self.rejected.append(f"{raw_category or '(无类别)'}: {'; '.join(problems)}")
            return {"accepted": False, "problems": problems}

        self.attribution = FailureAttribution(
            run_id=self.run.run_id,
            case_id=self.run.case_id,
            operation_id=self.run.operation_id,
            category=category,
            reason=reason[:600],
            evidence=evidence[:5],
            suggested_fix=str(payload.get("suggested_fix") or "")[:300],
        )
        return {"accepted": True, "category": category.value}


def _build_specs(context: DiagnosisContext) -> list[ToolSpec]:
    return [
        *spec_tool_specs(context),
        ToolSpec(
            name="get_run",
            description="取被诊断运行的完整事实：请求、脱敏后的响应、每条断言的观察与说明。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args: context.get_run(),
        ),
        ToolSpec(
            name="get_case",
            description="取该用例的定义（请求、参数取值与断言），用于判断是不是用例本身写错了。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args: context.get_case(),
        ),
        ToolSpec(
            name="list_related_runs",
            description="取同一 Operation 的其他历史运行，用于区分偶发失败与稳定失败。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args: context.list_related_runs(),
        ),
        ToolSpec(
            name="submit_diagnosis",
            description=(
                "提交归因。类别取 interface_defect / case_defect / environment / inconclusive 之一，"
                "必须给出 reason 与至少一条 evidence，否则会被拒绝。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [item.value for item in FailureCategory],
                    },
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "suggested_fix": {"type": "string"},
                },
                "required": ["category", "reason", "evidence"],
                "additionalProperties": False,
            },
            handler=lambda args: context.submit_diagnosis(args),
        ),
    ]


def _initial_message(context: DiagnosisContext) -> str:
    run = context.run
    return (
        f"用例 {run.case_id}（Operation {run.operation_id}）的结论是 {run.verdict.value}，"
        f"终止原因 {run.termination_reason.value}。\n"
        "请读取这次运行的完整事实与接口契约，判断问题出在接口、用例还是环境，然后提交一条带证据的归因。"
    )


def diagnose(
    specification: Specification,
    run: TestRun,
    *,
    model,
    case: TestCase | None = None,
    related_runs: list[TestRun] | None = None,
    budget: AgentBudget | None = None,
) -> tuple[AgentRun, FailureAttribution | None]:
    """对一条失败或无法判定的运行做归因。通过的运行不需要归因，会被直接拒绝。"""
    if run.verdict is Verdict.PASSED:
        raise ConfigError(f"运行 {run.run_id} 的结论是通过，不需要归因")
    if model is None:
        raise ConfigError("诊断需要模型端点：归因是判断，不存在确定性的替代实现")

    from .agent_loop import AgentLoop

    context = DiagnosisContext(specification, run, case=case, related_runs=related_runs)
    registry = ToolRegistry(context, specs=_build_specs(context))
    loop = AgentLoop(
        registry,
        model,
        system_prompt=DIAGNOSE_SYSTEM_PROMPT,
        initial_message=_initial_message(context),
        budget=budget,
        prompt_version=DIAGNOSE_PROMPT_VERSION,
        mode="diagnose",
    )
    outcome = loop.run()
    agent_run = outcome.agent_run

    attribution = context.attribution
    if attribution is None:
        agent_run.notes.append("模型没有提交归因")
        agent_run.notes.extend(context.rejected[:5])
        return agent_run, None

    attribution.model = agent_run.model
    attribution.prompt_version = agent_run.prompt_version
    attribution.agent_run_id = agent_run.run_id
    attribution.created_at = datetime.now(timezone.utc)
    agent_run.notes.extend(context.rejected[:5])
    return agent_run, attribution


__all__ = [
    "DIAGNOSE_PROMPT_VERSION",
    "DIAGNOSE_SYSTEM_PROMPT",
    "DiagnosisContext",
    "diagnose",
]
