"""aprobe 命令行。

命令边界就是循环边界：generate 与 run 之间隔着"用例文件被人审阅"这一步，
而 run 本身完全确定性、不调用模型。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .assertions import AssertionEvaluator
from .cases import dump_case_file, load_case_file, operations_by_id, order_cases, validate_cases
from .config import DEFAULT_CONFIG_NAME, Config, load_config
from .errors import AprobeError, CaseFileError, ConfigError, ExitCode, SpecError
from .evaluation import compare, evaluate_suite, gate, load_suite, render_comparison, render_report
from .models import (
    APROBE_VERSION,
    AgentBudget,
    RunProvenance,
    TerminationReason,
    TestCase,
    TestRun,
    Verdict,
)
from .diagnosis import diagnose
from .model_client import from_environment
from .planner import plan, planner_label
from .policy import TargetPolicy
from .report import RENDERERS, ReportMeta, gate_exit_code, summarize
from .runner import CredentialProvider, TestRunner, probe_baseline
from .specification import load_specification
from .tools import history_from_runs
from .trace import TraceStore


def _load_context(
    args: argparse.Namespace, *, require_config: bool, require_cases: bool
) -> tuple[Config | None, Path, Path, Path | None]:
    """配置可选，但缺省时命令行必须显式给出规范（以及用例，如果需要的话）。"""
    config_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG_NAME)
    config = load_config(config_path) if config_path.is_file() else None
    base = config_path.parent.resolve() if config is not None else Path.cwd()

    if require_config and config is None:
        raise ConfigError(f"未找到配置文件 {config_path}；该命令需要它来定位被测目标与允许范围")

    spec_arg = getattr(args, "spec", None)
    cases_arg = getattr(args, "cases", None)
    if config is not None:
        if spec_arg:
            config.spec = spec_arg
        if cases_arg:
            config.cases = cases_arg
        if getattr(args, "target", None):
            config.target.base_url = args.target
        if getattr(args, "allow_write", False):
            config.allow_write = True

    spec_path = Path(spec_arg) if spec_arg else (base / config.spec if config else None)
    cases_path = Path(cases_arg) if cases_arg else (base / config.cases if config else None)
    if spec_path is None:
        raise SpecError("需要 --config 或 --spec 指定 OpenAPI 规范")
    if require_cases and cases_path is None:
        raise CaseFileError("需要 --config 或 --cases 指定用例文件")
    return config, base, spec_path, cases_path


def cmd_generate(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=False, require_cases=False)
    out = Path(args.out) if args.out else cases_path
    if out is None:
        out = base / "cases" / "aprobe.yaml"

    existing: list[TestCase] = []
    if out.exists():
        if not args.force:
            raise CaseFileError(
                f"{out} 已存在。用例文件是审批载体（ADR-0001），aprobe 不静默覆盖它；"
                "确认 diff 后请加 --force（已有用例优先，新产物不会替换它们）"
            )
        existing = load_case_file(out)

    specification = load_specification(spec_path)

    store: TraceStore | None = None
    history: dict[str, list[dict[str, str]]] = {}
    if config is not None:
        store = TraceStore(config.resolve(base, config.trace_db))
        history = history_from_runs(store.list_runs())

    model = from_environment(dict(os.environ)) if args.mode in ("agent", "auto") else None
    budget = AgentBudget(
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_ms=int(args.max_seconds * 1000),
    )
    result = plan(
        specification,
        mode=args.mode,
        existing_cases=existing,
        history=history,
        budget=budget,
        model=model,
    )
    dump_case_file(out, result.cases)

    agent_run = result.agent_run
    if store is not None and agent_run is not None:
        store.record_agent_run(agent_run)

    added = len(result.cases) - len(existing)
    print(f"规范：{specification.title} {specification.version}（{len(specification.operations)} 个 Operation）")
    print(f"规划器：{agent_run.mode if agent_run else args.mode}（{args.mode}）")
    print(f"用例：{len(result.cases)} 条（新增 {added}）→ {out}")
    for case in result.cases[len(existing) :]:
        print(f"  + {case.id}  ← {case.request.method} {case.request.path} [{case.origin}]")
    if result.needs_input:
        print(f"未被覆盖的 {len(result.needs_input)} 个 Operation（未生成用例）：")
        for item in result.needs_input:
            print(f"  - {item.method} {item.path}：{item.reason}")
    for note in result.ignored:
        print(f"  规范中被忽略的部分：{note}")

    if agent_run is not None:
        print(
            f"Agent 循环：{agent_run.consumed_steps} 步，{agent_run.consumed_tokens} token，"
            f"{agent_run.consumed_ms}ms，终止原因 {agent_run.termination_reason.value}"
        )
        for note in agent_run.notes:
            print(f"  · {note}")
        if store is not None and config is not None:
            print(f"Agent 轨迹已记录到 {config.resolve(base, config.trace_db)}")
        if agent_run.termination_reason in (
            TerminationReason.BUDGET_EXHAUSTED,
            TerminationReason.PLANNER_FAILED,
        ):
            # 规划没跑完既不是通过也不是失败，用「无法判定」的退码让流水线能区分
            return ExitCode.INCONCLUSIVE

    if not result.cases:
        raise CaseFileError("没有生成任何用例，请检查规范中的 2xx 响应声明")
    return ExitCode.OK


def cmd_validate(args: argparse.Namespace) -> int:
    _, _, spec_path, cases_path = _load_context(args, require_config=False, require_cases=True)
    assert cases_path is not None

    specification = load_specification(spec_path)
    cases = load_case_file(cases_path)
    problems = validate_cases(cases, specification)
    print(f"规范：{specification.title} {specification.version}（{len(specification.operations)} 个 Operation）")
    print(f"用例：{len(cases)} 条（{cases_path}）")
    if problems:
        print("发现以下问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return ExitCode.INVALID_INPUT
    print("用例文件合法，且与规范一致。")
    return ExitCode.OK


def cmd_run(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=True, require_cases=True)
    assert config is not None and cases_path is not None

    specification = load_specification(spec_path)
    cases = load_case_file(cases_path)
    problems = validate_cases(cases, specification)
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise CaseFileError("用例文件非法，拒绝执行")

    by_id = operations_by_id(specification)
    policy = TargetPolicy(
        allow=config.target.allow,
        allow_write=config.allow_write,
        timeout_ms=config.limits.timeout_ms,
        max_response_bytes=config.limits.max_response_bytes,
        retries=config.limits.retries,
    )
    runner = TestRunner(
        policy=policy,
        evaluator=AssertionEvaluator(specification),
        credentials=CredentialProvider(config.auth.scheme, config.auth.env, config.auth.header),
        environ=dict(os.environ),
    )
    store = TraceStore(config.resolve(base, config.trace_db))
    provenance = RunProvenance(
        target=config.target.base_url,
        spec_source=str(spec_path),
        spec_title=specification.title,
        spec_version=specification.version,
        cases_file=str(cases_path),
        planner=planner_label(cases),
    )

    runs: list[TestRun] = []
    for case in order_cases(cases):
        run = runner.execute(case=case, operation=by_id[case.operation_id], provenance=provenance)
        store.record(run)
        runs.append(run)
        print(f"{run.verdict.value:>12}  {case.id}  ({run.duration_ms}ms, {run.termination_reason.value})")

    summary = summarize(runs)
    print(
        f"合计 {summary['total']}：通过 {summary['passed']}，失败 {summary['failed']}，"
        f"无法判定 {summary['inconclusive']}（其中策略拒绝 {summary['policy_denied']}）"
    )
    print(f"Trace 已记录到 {config.resolve(base, config.trace_db)}")

    meta = ReportMeta.from_runs(runs)
    for flag, fmt in (("json", "json"), ("junit", "junit"), ("markdown", "md")):
        destination = getattr(args, flag)
        if destination:
            print(f"报告已写入 {_write(Path(destination), RENDERERS[fmt](runs, meta))}")

    return gate_exit_code(runs, args.fail_on)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def cmd_evaluate(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=True, require_cases=False)
    assert config is not None

    suite = load_suite(args.suite)
    # 样例集里的相对路径与配置里的一致，都以配置文件所在目录为基准
    if args.spec is None:
        spec_path = config.resolve(base, suite.spec)
    if args.cases is None:
        cases_path = config.resolve(base, suite.cases)
    if cases_path is None:
        raise CaseFileError("评估需要用例文件")

    specification = load_specification(spec_path)
    policy = TargetPolicy(
        allow=config.target.allow,
        allow_write=config.allow_write,
        timeout_ms=config.limits.timeout_ms,
        max_response_bytes=config.limits.max_response_bytes,
        retries=config.limits.retries,
    )
    # 基准身份必须在跑之前确认：没声明基准的数字没有意义
    baseline = probe_baseline(policy, config.target.base_url, config.limits.timeout_ms)
    if baseline.get("scenario") != suite.scenario:
        raise ConfigError(
            f"基准场景不一致：样例集声明 {suite.scenario!r}，目标自述 {baseline.get('scenario')!r}。"
            "指标只在声明过的基准上才有意义，拒绝继续"
        )

    runner = TestRunner(
        policy=policy,
        evaluator=AssertionEvaluator(specification),
        credentials=CredentialProvider(config.auth.scheme, config.auth.env, config.auth.header),
        environ=dict(os.environ),
    )
    store = TraceStore(config.resolve(base, config.trace_db))
    by_id = operations_by_id(specification)

    def measure(case_file: Path):
        """用完全相同的方式度量一套用例——对比要公平，两侧必须走同一条路径。"""
        cases = load_case_file(case_file)
        problems = validate_cases(cases, specification)
        if problems:
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            raise CaseFileError(f"用例文件非法，拒绝评估：{case_file}")
        ordered = order_cases(cases)
        provenance = RunProvenance(
            target=config.target.base_url,
            spec_source=str(spec_path),
            spec_title=specification.title,
            spec_version=specification.version,
            cases_file=str(case_file),
            planner=planner_label(cases),
        )

        def run_once() -> list[TestRun]:
            produced: list[TestRun] = []
            for case in ordered:
                run = runner.execute(case=case, operation=by_id[case.operation_id], provenance=provenance)
                store.record(run)
                produced.append(run)
            return produced

        # 只统计"产出这批用例"的那次规划成本，而不是仓库里所有历史规划
        case_ids = {case.id for case in ordered}
        planning_runs = [run for run in store.list_agent_runs() if case_ids & set(run.produced_case_ids)]
        return evaluate_suite(
            suite,
            run_once=run_once,
            scenario_version=str(baseline.get("version", "")),
            repeat=args.repeat,
            declared_by=planner_label(cases),
            agent_steps=sum(run.consumed_steps for run in planning_runs),
            agent_tokens=sum(run.consumed_tokens for run in planning_runs),
        )

    reference = measure(cases_path)
    print(render_report(reference))
    print(f"Trace 已记录到 {config.resolve(base, config.trace_db)}")

    gated = reference
    comparison = None
    if args.against_cases:
        subject = measure(Path(args.against_cases))
        comparison = compare(reference, subject)
        print(render_comparison(comparison, suite.name))
        gated = subject
        print(f"Trace 已记录到 {config.resolve(base, config.trace_db)}")

    if args.json:
        payload = {"report": json.loads(reference.model_dump_json())}
        if comparison is not None:
            payload["comparison"] = json.loads(comparison.model_dump_json())
        print(f"评估结果已写入 {_write(Path(args.json), json.dumps(payload, ensure_ascii=False, indent=2))}")

    # 门禁只作用于"要交付的那套用例"（有对比时是右侧），参考侧只用来做比较
    code, reason = gate(gated, args.min_accuracy)
    if reason:
        print(f"aprobe: {reason}", file=sys.stderr)
    return code


def cmd_diagnose(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=True, require_cases=True)
    assert config is not None and cases_path is not None

    model = from_environment(dict(os.environ))
    if model is None:
        raise ConfigError(
            "诊断需要模型端点（设置 APROBE_MODEL_BASE_URL 与 APROBE_MODEL）。"
            "归因是判断而不是计算，因此没有降级模式的替代实现"
        )

    specification = load_specification(spec_path)
    cases = {case.id: case for case in load_case_file(cases_path)}
    store = TraceStore(config.resolve(base, config.trace_db))

    if args.run:
        subject = store.get(args.run)
        if subject is None:
            raise CaseFileError(f"Trace 中没有 run_id={args.run}")
        subjects = [subject]
    else:
        subjects = [run for run in store.list_runs() if run.verdict is not Verdict.PASSED]
    if not subjects:
        print("没有任何需要归因的运行（全部通过）。")
        return ExitCode.OK

    all_runs = store.list_runs()
    budget = AgentBudget(
        max_steps=args.max_steps, max_tokens=args.max_tokens, max_ms=int(args.max_seconds * 1000)
    )
    attributions = []
    missing = 0
    for run in subjects:
        related = [
            other for other in all_runs if other.operation_id == run.operation_id and other.run_id != run.run_id
        ]
        agent_run, attribution = diagnose(
            specification,
            run,
            model=model,
            case=cases.get(run.case_id),
            related_runs=related,
            budget=budget,
        )
        store.record_agent_run(agent_run)
        if attribution is None:
            missing += 1
            print(f"{run.case_id}: 没有得到归因（{agent_run.termination_reason.value}）")
            for note in agent_run.notes:
                print(f"  · {note}")
            continue
        store.record_attribution(attribution)
        attributions.append(attribution)
        print(f"{run.case_id}: {attribution.category.value} —— {attribution.reason}")
        for item in attribution.evidence:
            print(f"  · 证据：{item}")
        if attribution.suggested_fix:
            print(f"  · 方向：{attribution.suggested_fix}")

    print(f"归因 {len(attributions)}/{len(subjects)} 条，{missing} 条没有依据")
    print(f"Trace 已记录到 {config.resolve(base, config.trace_db)}")
    if args.json:
        payload = [json.loads(item.model_dump_json()) for item in attributions]
        print(f"归因已写入 {_write(Path(args.json), json.dumps(payload, ensure_ascii=False, indent=2))}")
    # 没有依据的归因既不是通过也不是失败：用「无法判定」的退码让流水线能区分
    return ExitCode.OK if missing == 0 else ExitCode.INCONCLUSIVE


def cmd_report(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=True, require_cases=False)
    assert config is not None

    # 报告完全从 Trace 推导（ADR-0002）：即使规范文件已经移动，历史报告仍然成立
    store = TraceStore(config.resolve(base, config.trace_db))
    if args.run:
        run = store.get(args.run)
        if run is None:
            raise CaseFileError(f"Trace 中没有 run_id={args.run}")
        runs = [run]
    else:
        runs = store.list_runs()
    if not runs:
        raise CaseFileError("Trace 中还没有任何运行记录")

    attributions = store.list_attributions()
    meta = ReportMeta.from_runs(runs)
    content = RENDERERS[args.format](runs, meta, attributions)
    if args.out:
        print(f"报告已写入 {_write(Path(args.out), content)}")
    else:
        print(content)
    return ExitCode.OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aprobe", description="面向 OpenAPI 的接口契约测试 Agent")
    parser.add_argument("--version", action="version", version=f"aprobe {APROBE_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--config", default=None, help=f"配置文件，默认 {DEFAULT_CONFIG_NAME}")
        sub.add_argument("--spec", default=None, help="覆盖配置文件中的 OpenAPI 规范路径")
        sub.add_argument("--cases", default=None, help="覆盖配置文件中的用例文件路径")

    generate = subparsers.add_parser("generate", help="以降级模式（不调用模型）生成用例文件")
    add_common(generate)
    generate.add_argument("--out", default=None, help="输出路径，默认写到配置中的 cases")
    generate.add_argument("--force", action="store_true", help="允许写入已存在的用例文件（已有用例优先保留）")
    generate.add_argument(
        "--mode",
        choices=("degraded", "agent", "auto"),
        default="degraded",
        help="degraded 不调用模型；agent 用有界 Agent 循环；auto 有模型走 agent，否则降级",
    )
    generate.add_argument("--max-steps", type=int, default=8, help="Agent 循环步数上限")
    generate.add_argument("--max-tokens", type=int, default=24000, help="Agent 循环 token 上限")
    generate.add_argument("--max-seconds", type=float, default=120.0, help="Agent 循环时长上限（秒）")
    generate.set_defaults(func=cmd_generate)

    validate = subparsers.add_parser("validate", help="离线校验用例文件（不联网、不调用模型）")
    add_common(validate)
    validate.set_defaults(func=cmd_validate)

    run = subparsers.add_parser("run", help="确定性地执行用例文件并记录 Trace")
    add_common(run)
    run.add_argument("--target", default=None, help="覆盖被测目标地址")
    run.add_argument("--allow-write", action="store_true", help="显式开启写操作（默认拒绝）")
    run.add_argument(
        "--fail-on",
        choices=("failed", "inconclusive", "none"),
        default="failed",
        help="哪些结论构成失败退出码，默认 failed",
    )
    run.add_argument("--json", default=None, help="导出 JSON 报告的路径")
    run.add_argument("--junit", default=None, help="导出 JUnit XML 报告的路径")
    run.add_argument("--markdown", default=None, help="导出 Markdown 报告的路径")
    run.set_defaults(func=cmd_run)

    evaluate = subparsers.add_parser("evaluate", help="在评估基准上回放标注样例集并输出指标")
    add_common(evaluate)
    evaluate.add_argument("--suite", required=True, help="评估样例集路径")
    evaluate.add_argument("--target", default=None, help="覆盖被测基准地址")
    evaluate.add_argument("--repeat", type=int, default=1, help="回放轮数，用于计算回放一致率")
    evaluate.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="准确率门槛；缺省时任何不一致都算失败（更严格）",
    )
    evaluate.add_argument(
        "--against-cases",
        default=None,
        help="第二套用例文件：在同一基准与同一套标注下对比两种规划器的差距",
    )
    evaluate.add_argument("--json", default=None, help="导出评估结果的路径")
    evaluate.set_defaults(func=cmd_evaluate)

    diagnose = subparsers.add_parser("diagnose", help="对失败或无法判定的运行做归因（需要模型端点）")
    add_common(diagnose)
    diagnose.add_argument("--run", default=None, help="只归因指定 run_id；缺省归因全部未通过的运行")
    diagnose.add_argument("--max-steps", type=int, default=6, help="归因循环步数上限")
    diagnose.add_argument("--max-tokens", type=int, default=24000, help="归因循环 token 上限")
    diagnose.add_argument("--max-seconds", type=float, default=90.0, help="归因循环时长上限（秒）")
    diagnose.add_argument("--json", default=None, help="导出归因的路径")
    diagnose.set_defaults(func=cmd_diagnose)

    report = subparsers.add_parser("report", help="从 Trace 导出报告")
    add_common(report)
    report.add_argument("--run", default=None, help="只导出指定 run_id")
    report.add_argument("--format", choices=sorted(RENDERERS), default="md")
    report.add_argument("--out", default=None, help="输出路径，缺省打印到标准输出")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AprobeError as exc:
        print(f"aprobe: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        print("aprobe: 已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
