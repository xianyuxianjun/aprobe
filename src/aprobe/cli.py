"""aprobe 命令行。

命令边界就是循环边界：generate 与 run 之间隔着"用例文件被人审阅"这一步，
而 run 本身完全确定性、不调用模型。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .assertions import AssertionEvaluator
from .cases import dump_case_file, load_case_file, operations_by_id, order_cases, validate_cases
from .config import DEFAULT_CONFIG_NAME, Config, load_config
from .errors import AprobeError, CaseFileError, ConfigError, ExitCode, SpecError
from .generator import generate_cases
from .models import APROBE_VERSION, DETERMINISTIC_PLANNER_VERSION, RunProvenance, TestRun
from .policy import TargetPolicy
from .report import RENDERERS, ReportMeta, gate_exit_code, summarize
from .runner import CredentialProvider, TestRunner
from .specification import load_specification
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

    if out.exists() and not args.force:
        raise CaseFileError(
            f"{out} 已存在。用例文件是审批载体（ADR-0001），aprobe 不静默覆盖它；确认后请加 --force"
        )

    specification = load_specification(spec_path)
    result = generate_cases(specification)
    dump_case_file(out, result.cases)

    print(f"规范：{specification.title} {specification.version}（{len(specification.operations)} 个 Operation）")
    print(f"降级模式生成 {len(result.cases)} 条用例 → {out}")
    for case in result.cases:
        print(f"  - {case.id}  ← {case.request.method} {case.request.path}")
    if result.needs_input:
        print(f"需要人工输入的 {len(result.needs_input)} 个 Operation（未生成用例）：")
        for item in result.needs_input:
            print(f"  - {item.method} {item.path}：{item.reason}")
    for note in result.ignored:
        print(f"  规范中被忽略的部分：{note}")
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
        planner=DETERMINISTIC_PLANNER_VERSION,
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


def cmd_report(args: argparse.Namespace) -> int:
    config, base, spec_path, cases_path = _load_context(args, require_config=True, require_cases=False)
    assert config is not None

    specification = load_specification(spec_path)
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

    meta = ReportMeta.from_runs(runs)
    content = RENDERERS[args.format](runs, meta)
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
    generate.add_argument("--force", action="store_true", help="允许覆盖已存在的用例文件")
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
