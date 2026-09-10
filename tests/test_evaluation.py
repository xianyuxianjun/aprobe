"""评估的度量计算与评估命令的端到端行为。

度量部分是纯计算，所以测试它不需要任何 mock；
端到端部分才用真实的基准服务，并把"量错基准"也当成一种要挡住的错误。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from conftest import write_config

from aprobe.cli import main
from aprobe.evaluation import EvalSuite, Expectation, evaluate_suite, gate, load_suite, render_report
from aprobe.models import Observation, TerminationReason, TestRun, Verdict

REPO = Path(__file__).resolve().parents[1]
CONFORMANT_SUITE = REPO / "eval" / "petstore-conformant.yaml"
VIOLATING_SUITE = REPO / "eval" / "petstore-violating.yaml"
EDGE_CONFORMANT_SUITE = REPO / "eval" / "edgecases-conformant.yaml"
EDGE_VIOLATING_SUITE = REPO / "eval" / "edgecases-violating.yaml"


def run(case_id: str, verdict: Verdict, *, evidence: int = 1, duration_ms: int = 10) -> TestRun:
    from aprobe.models import AssertionResult, Assertion, AssertionKind

    results = [
        AssertionResult(
            assertion=Assertion(kind=AssertionKind.STATUS, **{"in": [200]}),
            verdict=verdict,
            observed="status=200",
        )
        for _ in range(evidence)
    ]
    return TestRun(
        run_id=f"run-{case_id}",
        case_id=case_id,
        operation_id="listPets",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_ms=duration_ms,
        verdict=verdict,
        termination_reason=TerminationReason.COMPLETED,
        observation=Observation(status_code=200),
        assertion_results=results,
    )


def suite(*expectations: tuple[str, Verdict]) -> EvalSuite:
    return EvalSuite(
        name="unit",
        scenario="conformant",
        spec="examples/petstore.yaml",
        cases="cases/petstore.yaml",
        expectations=[Expectation(case_id=case_id, verdict=verdict) for case_id, verdict in expectations],
    )


def test_perfect_agreement_scores_one() -> None:
    report = evaluate_suite(
        suite(("a", Verdict.PASSED), ("b", Verdict.FAILED)),
        run_once=lambda: [run("a", Verdict.PASSED), run("b", Verdict.FAILED)],
    )
    assert report.metrics.total == 2
    assert report.metrics.matched == 2
    assert report.metrics.accuracy == 1.0
    assert report.metrics.false_positives == 0
    assert report.metrics.false_negatives == 0
    assert report.metrics.evidence_coverage == 1.0


def test_false_positive_and_false_negative_are_counted_separately() -> None:
    report = evaluate_suite(
        suite(("a", Verdict.PASSED), ("b", Verdict.FAILED)),
        run_once=lambda: [run("a", Verdict.FAILED), run("b", Verdict.PASSED)],
    )
    assert report.metrics.accuracy == 0.0
    assert report.metrics.false_positives == 1  # 标注是通过，实际失败
    assert report.metrics.false_negatives == 1  # 标注是失败，实际通过


def test_inconclusive_is_not_counted_as_agreement() -> None:
    report = evaluate_suite(
        suite(("a", Verdict.PASSED)),
        run_once=lambda: [run("a", Verdict.INCONCLUSIVE, evidence=0)],
    )
    assert report.metrics.matched == 0
    assert report.metrics.inconclusive == 1


def test_conclusion_without_evidence_is_flagged() -> None:
    """结构上不该发生：没有断言结果却给出了通过/失败。发生了就要在指标里现形。"""
    report = evaluate_suite(
        suite(("a", Verdict.PASSED)),
        run_once=lambda: [run("a", Verdict.PASSED, evidence=0)],
    )
    assert report.metrics.unsupported_conclusions == 1
    assert report.metrics.evidence_coverage == 0.0


def test_repeated_replay_measures_consistency() -> None:
    rounds = [[run("a", Verdict.PASSED), run("b", Verdict.PASSED)], [run("a", Verdict.PASSED), run("b", Verdict.FAILED)]]
    calls = {"index": 0}

    def run_once():
        produced = rounds[min(calls["index"], len(rounds) - 1)]
        calls["index"] += 1
        return produced

    report = evaluate_suite(suite(("a", Verdict.PASSED), ("b", Verdict.FAILED)), run_once=run_once, repeat=2)
    assert report.metrics.consistency == 0.5
    assert report.metrics.matched == 1  # 指标只看第一轮


def test_unnannotated_cases_are_excluded_and_reported() -> None:
    report = evaluate_suite(
        suite(("a", Verdict.PASSED)),
        run_once=lambda: [run("a", Verdict.PASSED), run("unlabelled", Verdict.PASSED)],
    )
    assert report.metrics.total == 1
    assert any("没有标注预期结论" in note for note in report.notes)


def test_markdown_report_declares_its_limits() -> None:
    report = evaluate_suite(suite(("a", Verdict.PASSED)), run_once=lambda: [run("a", Verdict.PASSED)])
    text = render_report(report)
    assert "判定准确率" in text
    assert "不构成对任意接口的准确率声明" in text


def test_bundled_suites_load_and_agree_on_their_labels() -> None:
    conformant = load_suite(CONFORMANT_SUITE)
    violating = load_suite(VIOLATING_SUITE)
    assert conformant.scenario == "conformant"
    assert violating.scenario == "violating"
    assert {item.case_id for item in conformant.expectations} == {item.case_id for item in violating.expectations}
    # 两个样例集只有一条判定不同——这正是"注入的违约"
    diff = [
        item.case_id
        for item in conformant.expectations
        if item.verdict is not violating.expected_for(item.case_id)
    ]
    assert diff == ["get-pet-stats"]


def test_evaluate_scores_the_conformant_baseline(tmp_path, spec_path, cases_path, conformant, capsys) -> None:
    (tmp_path / "examples").mkdir()
    shutil.copy(spec_path, tmp_path / "examples" / "petstore.yaml")
    (tmp_path / "cases").mkdir()
    shutil.copy(cases_path, tmp_path / "cases" / "petstore.yaml")
    config = write_config(tmp_path, base_url=conformant.base_url, spec=spec_path, cases=cases_path)

    code = main(["evaluate", "--config", str(config), "--suite", str(CONFORMANT_SUITE), "--repeat", "2"])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "判定准确率 | 100.0%" in output
    assert "回放一致率 | 100.0%" in output
    assert "无依据结论 | 0" in output
    assert "基准：`conformant`（版本 1.0.0）" in output


def test_evaluate_detects_the_injected_violation(tmp_path, spec_path, cases_path, violating, capsys) -> None:
    (tmp_path / "examples").mkdir()
    shutil.copy(spec_path, tmp_path / "examples" / "petstore.yaml")
    (tmp_path / "cases").mkdir()
    shutil.copy(cases_path, tmp_path / "cases" / "petstore.yaml")
    config = write_config(tmp_path, base_url=violating.base_url, spec=spec_path, cases=cases_path)

    code = main(["evaluate", "--config", str(config), "--suite", str(VIOLATING_SUITE)])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "判定准确率 | 100.0%" in output


def test_evaluate_refuses_to_measure_against_the_wrong_baseline(
    tmp_path, spec_path, cases_path, violating, capsys
) -> None:
    """用 violating 的进程去跑 conformant 的样例集，指标就没有意义，必须拒绝。"""
    config = write_config(tmp_path, base_url=violating.base_url, spec=spec_path, cases=cases_path)
    code = main(
        [
            "evaluate",
            "--config",
            str(config),
            "--suite",
            str(CONFORMANT_SUITE),
            "--spec",
            str(spec_path),
            "--cases",
            str(cases_path),
        ]
    )
    assert code == 3
    assert "基准场景不一致" in capsys.readouterr().err


def test_evaluate_fails_when_labels_disagree_with_reality(tmp_path, spec_path, cases_path, conformant, capsys) -> None:
    """把 violating 的标注用在 conformant 基准上：指标必须掉下来并且非零退出。"""
    (tmp_path / "examples").mkdir()
    shutil.copy(spec_path, tmp_path / "examples" / "petstore.yaml")
    (tmp_path / "cases").mkdir()
    shutil.copy(cases_path, tmp_path / "cases" / "petstore.yaml")
    config = write_config(tmp_path, base_url=conformant.base_url, spec=spec_path, cases=cases_path)

    mismatched = tmp_path / "wrong-labels.yaml"
    mismatched.write_text(
        VIOLATING_SUITE.read_text(encoding="utf-8").replace("scenario: violating", "scenario: conformant"),
        encoding="utf-8",
    )
    code = main(["evaluate", "--config", str(config), "--suite", str(mismatched)])
    output = capsys.readouterr().out
    assert code == 1, output
    assert "假阴性（应为失败，实际通过） | 1" in output


def test_evaluate_exports_machine_readable_result(tmp_path, spec_path, cases_path, conformant, capsys) -> None:
    config = write_config(tmp_path, base_url=conformant.base_url, spec=spec_path, cases=cases_path)
    out = tmp_path / "eval.json"
    code = main(
        [
            "evaluate",
            "--config",
            str(config),
            "--suite",
            str(CONFORMANT_SUITE),
            "--spec",
            str(spec_path),
            "--cases",
            str(cases_path),
            "--json",
            str(out),
        ]
    )
    assert code == 0, capsys.readouterr().out
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["metrics"]["accuracy"] == 1.0
    assert payload["scenario_version"] == "1.0.0"
    assert len(payload["outcomes"]) == 7


def test_all_four_bundled_suites_load() -> None:
    suites = {
        path.name: load_suite(path)
        for path in (CONFORMANT_SUITE, VIOLATING_SUITE, EDGE_CONFORMANT_SUITE, EDGE_VIOLATING_SUITE)
    }
    assert suites["petstore-conformant.yaml"].scenario == "conformant"
    assert suites["petstore-violating.yaml"].scenario == "violating"
    assert suites["edgecases-conformant.yaml"].scenario == "conformant"
    assert suites["edgecases-violating.yaml"].scenario == "violating"
    assert len(suites["edgecases-violating.yaml"].expectations) == 12


def test_edge_suites_differ_in_more_than_one_place() -> None:
    """第二个基准的意义就在这里：多种偏差各自独立地反映在标注差异上。"""
    conformant = {item.case_id: item.verdict for item in load_suite(EDGE_CONFORMANT_SUITE).expectations}
    violating = {item.case_id: item.verdict for item in load_suite(EDGE_VIOLATING_SUITE).expectations}
    diff = sorted(case_id for case_id in conformant if conformant[case_id] is not violating[case_id])
    assert len(diff) == 10
    assert violating["get-broken-json"] is Verdict.INCONCLUSIVE


def test_gate_always_fails_on_a_false_negative() -> None:
    """漏报违约是最坏的错误：即使准确率很高，也不能被平均掉。"""
    report = evaluate_suite(
        suite(("a", Verdict.PASSED), ("b", Verdict.FAILED)),
        run_once=lambda: [run("a", Verdict.PASSED), run("b", Verdict.PASSED)],
    )
    code, reason = gate(report)
    assert code == 1
    assert "漏报" in reason


def test_gate_threshold_tolerates_mismatches() -> None:
    report = evaluate_suite(
        suite(("a", Verdict.PASSED), ("b", Verdict.PASSED)),
        run_once=lambda: [run("a", Verdict.PASSED), run("b", Verdict.FAILED)],
    )
    assert gate(report)[0] == 1  # 缺省严格模式：任何不一致都失败
    assert gate(report, 0.4)[0] == 0  # 50% 达到 40% 门槛
    assert gate(report, 0.6)[0] == 1  # 50% 未达 60% 门槛


def test_gate_passes_a_perfect_report() -> None:
    report = evaluate_suite(suite(("a", Verdict.PASSED)), run_once=lambda: [run("a", Verdict.PASSED)])
    assert gate(report) == (0, "")


def test_edge_baseline_conformant_is_clean(tmp_path, edge_spec_path, edge_cases_path, edge_conformant, capsys) -> None:
    config = write_config(tmp_path, base_url=edge_conformant.base_url, spec=edge_spec_path, cases=edge_cases_path)
    code = main(
        [
            "evaluate",
            "--config",
            str(config),
            "--suite",
            str(EDGE_CONFORMANT_SUITE),
            "--spec",
            str(edge_spec_path),
            "--cases",
            str(edge_cases_path),
            "--repeat",
            "2",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "覆盖用例 | 12" in output
    assert "判定准确率 | 100.0%" in output


def test_edge_baseline_violating_catches_every_injected_deviation(
    tmp_path, edge_spec_path, edge_cases_path, edge_violating, capsys
) -> None:
    config = write_config(tmp_path, base_url=edge_violating.base_url, spec=edge_spec_path, cases=edge_cases_path)
    code = main(
        [
            "evaluate",
            "--config",
            str(config),
            "--suite",
            str(EDGE_VIOLATING_SUITE),
            "--spec",
            str(edge_spec_path),
            "--cases",
            str(edge_cases_path),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "判定准确率 | 100.0%" in output
    assert "假阴性（应为失败，实际通过） | 0" in output
    assert "无法判定 | 1" in output  # 响应不是 JSON：无法校验，只能说无法判定
