from __future__ import annotations

import json
import socket
from pathlib import Path

from aprobe.cli import main


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_generate_needs_only_a_spec(tmp_path: Path, spec_path: Path, capsys) -> None:
    out = tmp_path / "cases.yaml"
    code = main(["generate", "--spec", str(spec_path), "--out", str(out)])
    assert code == 0
    assert out.is_file()
    output = capsys.readouterr().out
    assert "降级模式生成 3 条用例" in output
    assert "需要人工输入的 3 个 Operation" in output


def test_generate_refuses_to_overwrite_the_approval_carrier(tmp_path: Path, spec_path: Path, capsys) -> None:
    out = tmp_path / "cases.yaml"
    assert main(["generate", "--spec", str(spec_path), "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["generate", "--spec", str(spec_path), "--out", str(out)]) == 3
    assert "--force" in capsys.readouterr().err
    assert main(["generate", "--spec", str(spec_path), "--out", str(out), "--force"]) == 0


def test_validate_accepts_committed_cases(spec_path: Path, cases_path: Path, capsys) -> None:
    assert main(["validate", "--spec", str(spec_path), "--cases", str(cases_path)]) == 0
    assert "合法" in capsys.readouterr().out


def test_validate_reports_problems(tmp_path: Path, spec_path: Path, capsys) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        """
version: 1
cases:
  - id: list-pets
    operation_id: listPets
    request: {method: GET, path: /old-pets}
    assertions:
      - kind: status
        in: [200]
  - id: list-pets
    operation_id: nope
    request: {method: GET, path: /pets}
    assertions: []
""",
        encoding="utf-8",
    )
    assert main(["validate", "--spec", str(spec_path), "--cases", str(broken)]) == 3
    err = capsys.readouterr().err
    assert "重复" in err or "路径" in err


def test_run_against_conformant_baseline_succeeds(config_factory, conformant, tmp_path: Path, capsys) -> None:
    config = config_factory(base_url=conformant.base_url)
    report = tmp_path / "report.json"
    code = main(["run", "--config", str(config), "--json", str(report)])
    assert code == 0
    assert "失败 0" in capsys.readouterr().out
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["passed"] == 7


def test_run_detects_contract_violation(config_factory, violating, capsys) -> None:
    config = config_factory(base_url=violating.base_url)
    assert main(["run", "--config", str(config)]) == 1
    assert "get-pet-stats" in capsys.readouterr().out


def test_fail_on_none_still_reports_but_exits_zero(config_factory, violating, capsys) -> None:
    config = config_factory(base_url=violating.base_url)
    assert main(["run", "--config", str(config), "--fail-on", "none"]) == 0
    assert "失败 1" in capsys.readouterr().out


def test_unreachable_target_exits_with_inconclusive_code(config_factory, capsys) -> None:
    config = config_factory(base_url=f"http://127.0.0.1:{closed_port()}")
    assert main(["run", "--config", str(config)]) == 2
    assert "无法判定 7" in capsys.readouterr().out


def test_disallowed_target_exits_with_policy_code(config_factory, conformant, capsys) -> None:
    config = config_factory(base_url=conformant.base_url, allow=["example.com"])
    assert main(["run", "--config", str(config)]) == 4
    assert "策略拒绝 7" in capsys.readouterr().out


def test_missing_config_is_reported_as_invalid_input(capsys) -> None:
    assert main(["run", "--config", "/nonexistent/aprobe.yaml"]) == 3
    assert "未找到配置文件" in capsys.readouterr().err


def test_report_command_reads_the_trace(config_factory, conformant, capsys) -> None:
    config = config_factory(base_url=conformant.base_url)
    assert main(["run", "--config", str(config)]) == 0
    capsys.readouterr()
    assert main(["report", "--config", str(config), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["passed"] == 7
    assert payload["meta"]["target"] == conformant.base_url


def test_junit_report_is_written(config_factory, conformant, tmp_path: Path) -> None:
    config = config_factory(base_url=conformant.base_url)
    junit = tmp_path / "junit.xml"
    assert main(["run", "--config", str(config), "--junit", str(junit)]) == 0
    assert "<testsuite" in junit.read_text(encoding="utf-8")
