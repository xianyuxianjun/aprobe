from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml
from mock_service import MockService

from aprobe.specification import load_specification

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return ROOT


#: 测试期间必须清掉的变量：开发者本机可能真的导出了它们
_AMBIENT_VARIABLES = (
    "APROBE_MODEL_BASE_URL",
    "APROBE_MODEL",
    "APROBE_MODEL_API_KEY",
    "APROBE_MODEL_TIMEOUT_MS",
)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment():
    """让测试不受开发者本机状态影响。

    没有这条守卫时，仓库根目录下一份真实的 `.env` 或一个已导出的密钥就会改变
    测试结果——实测中它确实让一条断言"没有模型端点"的用例失败了。
    """
    saved = {name: os.environ.pop(name, None) for name in _AMBIENT_VARIABLES}
    previous = os.environ.get("APROBE_NO_ENV_FILE")
    os.environ["APROBE_NO_ENV_FILE"] = "1"
    yield
    os.environ.pop("APROBE_NO_ENV_FILE", None)
    if previous is not None:
        os.environ["APROBE_NO_ENV_FILE"] = previous
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def _guard_repository_fixtures():
    """测试不得改写仓库里被跟踪的样例文件。

    agent 模式会 --force 回写用例文件，而用例文件与规范都是仓库资产：
    测试里只要顺手指向仓库路径，就会在无人察觉的情况下污染版本库。这条守卫让
    那种错误在会话结束时报错，而不是等到 git status 才发现。
    """

    def checksums() -> dict[Path, str]:
        targets = sorted(ROOT.glob("cases/*.yaml")) + sorted(ROOT.glob("examples/*.yaml"))
        return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in targets}

    before = checksums()
    yield
    after = checksums()
    changed = sorted(str(path.relative_to(ROOT)) for path in before if before[path] != after.get(path))
    assert not changed, f"测试改写了仓库里的样例文件：{changed}"


@pytest.fixture(scope="session")
def spec_path() -> Path:
    return ROOT / "examples" / "petstore.yaml"


@pytest.fixture(scope="session")
def cases_path() -> Path:
    return ROOT / "cases" / "petstore.yaml"


@pytest.fixture(scope="session")
def conformant() -> MockService:
    service = MockService("conformant").start()
    yield service
    service.stop()


@pytest.fixture(scope="session")
def violating() -> MockService:
    service = MockService("violating").start()
    yield service
    service.stop()


@pytest.fixture(scope="session")
def edge_spec_path() -> Path:
    return ROOT / "examples" / "edgecases.yaml"


@pytest.fixture(scope="session")
def edge_cases_path() -> Path:
    return ROOT / "cases" / "edgecases.yaml"


@pytest.fixture
def edge_specification(edge_spec_path: Path):
    return load_specification(edge_spec_path)


def _edge_service(scenario: str):
    from edge_service import MockService as EdgeService

    return EdgeService(scenario).start()


@pytest.fixture(scope="session")
def edge_conformant():
    service = _edge_service("conformant")
    yield service
    service.stop()


@pytest.fixture(scope="session")
def edge_violating():
    service = _edge_service("violating")
    yield service
    service.stop()


@pytest.fixture
def specification(spec_path: Path):
    return load_specification(spec_path)


@pytest.fixture
def config_factory(tmp_path: Path, spec_path: Path, cases_path: Path):
    def build(**overrides) -> Path:
        parameters = {"spec": spec_path, "cases": cases_path}
        parameters.update(overrides)
        return write_config(tmp_path, **parameters)

    return build


def write_config(
    directory: Path,
    *,
    base_url: str,
    spec: Path,
    cases: Path,
    allow: list[str] | None = None,
    allow_write: bool = False,
) -> Path:
    path = directory / "aprobe.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "spec": str(spec),
                "cases": str(cases),
                "trace_db": str(directory / ".aprobe" / "trace.db"),
                "target": {"base_url": base_url, "allow": allow if allow is not None else ["127.0.0.1"]},
                "limits": {"timeout_ms": 3000, "max_response_bytes": 65536, "retries": 0},
                "auth": {"scheme": "bearer", "env": "APROBE_TEST_TOKEN"},
                "allow_write": allow_write,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
