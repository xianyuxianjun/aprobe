"""`.env` 支持。

它只是"环境变量的另一个来源"，所以这里要守住两条：
真实环境必须能覆盖它；有问题的行必须被忽略而不是让整个文件失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config
from model_server import ModelService

from aprobe.cli import main
from aprobe.config import effective_environment, parse_env_file


def test_parse_handles_the_usual_shapes() -> None:
    text = """
# 整行注释
   # 缩进的注释
export EXPORTED=value
PLAIN=value
QUOTED="has spaces"
SINGLE='single'
EMPTY=
WITH_EQUALS=a=b=c
SPACED =  trimmed
NOT_A_PAIR
BAD KEY=value
DUP=first
DUP=second
"""
    assert parse_env_file(text) == {
        "EXPORTED": "value",
        "PLAIN": "value",
        "QUOTED": "has spaces",
        "SINGLE": "single",
        "EMPTY": "",
        "WITH_EQUALS": "a=b=c",
        "SPACED": "trimmed",
        "DUP": "second",
    }


def test_parse_of_an_empty_or_junk_file_is_empty() -> None:
    assert parse_env_file("") == {}
    assert parse_env_file("\n\n# only comments\n") == {}


def test_real_environment_wins_over_the_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SHARED=from-file\nONLY_FILE=file\n", encoding="utf-8")
    result = effective_environment(tmp_path, {"SHARED": "from-real-environment", "ONLY_REAL": "real"})
    assert result["SHARED"] == "from-real-environment"  # 临时 FOO=bar 必须能覆盖 .env
    assert result["ONLY_FILE"] == "file"
    assert result["ONLY_REAL"] == "real"


def test_missing_file_changes_nothing(tmp_path: Path) -> None:
    assert effective_environment(tmp_path, {"A": "1"}) == {"A": "1"}


def test_env_file_is_not_required_to_be_utf8_friendly_junk_safe(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("===\n=no key\n1INVALID=x\nGOOD=y\n", encoding="utf-8")
    assert effective_environment(tmp_path, {}) == {"GOOD": "y"}


@pytest.fixture
def local_cases(tmp_path: Path, cases_path: Path) -> Path:
    """把用例文件复制到 tmp：测试绝不能写仓库里被跟踪的文件。

    agent 模式会 --force 回写用例文件，直接指向仓库里的那份会污染版本库。
    """
    target = tmp_path / "cases.yaml"
    target.write_text(cases_path.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture
def model_service():
    script = [
        {
            "content": "提交一条用例。",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "submit_case",
                    "arguments": {
                        "id": "list-pets-from-env-file",
                        "operation_id": "listPets",
                        "summary": "由 .env 驱动的 Agent 生成",
                        "request": {"method": "GET", "path": "/pets"},
                        "assertions": [{"kind": "status", "in": [200]}],
                    },
                }
            ],
        },
        {"content": "完成。"},
    ]
    service = ModelService(script).start()
    yield service
    service.stop()


def test_cli_reads_the_env_file_from_the_working_directory(
    tmp_path, spec_path, local_cases, model_service, monkeypatch, capsys
) -> None:
    """没有 export 任何东西，只靠 ./.env —— 这条路必须真的通。"""
    config = write_config(tmp_path, base_url="http://127.0.0.1:1", spec=spec_path, cases=local_cases)
    (tmp_path / ".env").write_text(
        f"APROBE_MODEL_BASE_URL={model_service.base_url}\nAPROBE_MODEL=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for name in ("APROBE_MODEL_BASE_URL", "APROBE_MODEL", "APROBE_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 0
    output = capsys.readouterr().out
    assert "规划器：agent" in output
    assert "list-pets-from-env-file" in output


def test_real_environment_still_beats_the_env_file(
    tmp_path, spec_path, local_cases, model_service, monkeypatch, capsys
) -> None:
    """`.env` 里是正确的端点，真实环境里是坏的 —— 必须用坏的那个（真实环境优先）。"""
    config = write_config(tmp_path, base_url="http://127.0.0.1:1", spec=spec_path, cases=local_cases)
    (tmp_path / ".env").write_text(
        f"APROBE_MODEL_BASE_URL={model_service.base_url}\nAPROBE_MODEL=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APROBE_MODEL_BASE_URL", "http://127.0.0.1:1")

    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 2
    assert "planner_failed" in capsys.readouterr().out


def test_env_file_values_never_reach_the_trace(
    tmp_path, spec_path, local_cases, model_service, monkeypatch, capsys
) -> None:
    """`.env` 里可能有真的密钥；它不能出现在任何落库产物里。"""
    config = write_config(tmp_path, base_url="http://127.0.0.1:1", spec=spec_path, cases=local_cases)
    (tmp_path / ".env").write_text(
        f"APROBE_MODEL_BASE_URL={model_service.base_url}\n"
        "APROBE_MODEL=from-env-file\n"
        "APROBE_MODEL_API_KEY=sk-super-secret-value\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["generate", "--config", str(config), "--mode", "agent", "--force"]) == 0
    capsys.readouterr()

    dumped = (tmp_path / ".aprobe" / "trace.db").read_bytes()
    assert b"sk-super-secret-value" not in dumped
