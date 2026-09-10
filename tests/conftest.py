from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from mock_service import MockService

from aprobe.specification import load_specification

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return ROOT


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
