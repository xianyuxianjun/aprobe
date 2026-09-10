from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone

from aprobe.models import Observation, RunProvenance, TerminationReason, TestRun, Verdict
from aprobe.report import ReportMeta, gate_exit_code, render_json, render_junit, render_markdown, summarize


def make_run(case_id: str, verdict: Verdict, reason: TerminationReason = TerminationReason.COMPLETED) -> TestRun:
    return TestRun(
        run_id=f"run-{case_id}",
        case_id=case_id,
        operation_id="listPets",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_ms=12,
        verdict=verdict,
        termination_reason=reason,
        request={"method": "GET", "url": "http://127.0.0.1:8080/pets"},
        observation=Observation(status_code=200, body_text="{}", body_json={}),
        assertion_results=[],
        provenance=RunProvenance(
            target="http://127.0.0.1:8080",
            spec_source="examples/petstore.yaml",
            spec_title="Petstore Baseline",
            spec_version="1.0.0",
            cases_file="cases/petstore.yaml",
        ),
    )


def runs() -> list[TestRun]:
    return [
        make_run("a", Verdict.PASSED),
        make_run("b", Verdict.FAILED),
        make_run("c", Verdict.INCONCLUSIVE, TerminationReason.POLICY_DENIED),
    ]


def test_summarize_counts_verdicts_and_reasons() -> None:
    summary = summarize(runs())
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["inconclusive"] == 1
    assert summary["policy_denied"] == 1


def test_markdown_declares_limits_and_lists_unfinished_cases() -> None:
    text = render_markdown(runs(), ReportMeta.from_runs(runs()))
    assert "结果不等于完整保证" in text
    assert "`b`" in text and "`c`" in text
    assert "| 通过 | 1 |" in text


def test_json_report_is_machine_readable() -> None:
    payload = json.loads(render_json(runs(), ReportMeta.from_runs(runs())))
    assert payload["summary"]["total"] == 3
    assert [item["case_id"] for item in payload["runs"]] == ["a", "b", "c"]


def test_gate_exit_code_priorities() -> None:
    passed = make_run("a", Verdict.PASSED)
    failed = make_run("b", Verdict.FAILED)
    inconclusive = make_run("c", Verdict.INCONCLUSIVE, TerminationReason.REQUEST_FAILED)
    denied = make_run("d", Verdict.INCONCLUSIVE, TerminationReason.POLICY_DENIED)

    assert gate_exit_code([passed]) == 0
    assert gate_exit_code([passed, failed]) == 1
    assert gate_exit_code([passed, inconclusive]) == 2
    assert gate_exit_code([passed, inconclusive, failed]) == 1
    assert gate_exit_code([passed, denied, failed]) == 4

    assert gate_exit_code([passed, inconclusive], "inconclusive") == 1
    assert gate_exit_code([passed, failed], "none") == 0
    assert gate_exit_code([passed, denied], "none") == 4


def test_junit_report_maps_verdicts_to_elements() -> None:
    root = ElementTree.fromstring(render_junit(runs(), ReportMeta.from_runs(runs())))
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"
    assert root.attrib["skipped"] == "1"
    names = [item.attrib["name"] for item in root.findall("testcase")]
    assert names == ["a", "b", "c"]
