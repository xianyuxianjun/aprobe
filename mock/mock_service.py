"""评估基准 #1：Petstore（契约结构与业务规则的正常/违约对照）。

边界：它是计量基准，不是能力上限——它让指标可复现，但不代表 aprobe 只能测它。
场景是版本化的：同一场景版本必须给出完全相同的响应，否则指标不可比。
"""

from __future__ import annotations

from typing import Any

from http_kit import MockResponse, Service, run_forever

SCENARIO_VERSION = "1.0.0"
SCENARIOS = ("conformant", "violating")

_PETS = {1: "Ada", 2: "Bao"}


def _pet(pid: int) -> dict[str, object]:
    return {
        "id": pid,
        "name": _PETS[pid],
        "owner": {"email": f"owner{pid}@example.com", "token": "mock-secret-token"},
    }


class PetstoreBehaviour:
    def __init__(self, scenario: str = "conformant") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景: {scenario}")
        self.scenario = scenario
        self.version = SCENARIO_VERSION

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: Any | None
    ) -> MockResponse:
        parts = [segment for segment in path.split("/") if segment]
        authorized = "authorization" in {key.lower() for key in headers}

        if method == "GET" and parts == ["health"]:
            return MockResponse(200, {"status": "ok"})

        if method == "GET" and parts == ["pets"]:
            return MockResponse(200, {"items": [_pet(pid) for pid in sorted(_PETS)], "total": len(_PETS)})

        if method == "GET" and parts == ["pets", "stats"]:
            total: object = len(_PETS)
            if self.scenario == "violating":
                total = str(len(_PETS))  # 契约声明 total 是 integer，这里故意违约
            return MockResponse(200, {"total": total, "as_of": "2026-01-01T00:00:00Z"})

        if method == "GET" and len(parts) == 3 and parts[0] == "pets" and parts[2] == "owners":
            if not parts[1].isdigit() or int(parts[1]) not in _PETS:
                return MockResponse(404, {"error": "pet not found"})
            if not authorized:
                return MockResponse(401, {"error": "missing credential"})
            return MockResponse(200, {"email": f"owner{parts[1]}@example.com", "token": "mock-secret-token"})

        if method == "GET" and len(parts) == 2 and parts[0] == "pets":
            if not parts[1].isdigit() or int(parts[1]) not in _PETS:
                return MockResponse(404, {"error": "pet not found"})
            return MockResponse(200, _pet(int(parts[1])))

        if method == "POST" and parts == ["pets"]:
            if not isinstance(body, dict) or not body.get("name"):
                return MockResponse(422, {"error": "name is required"})
            return MockResponse(201, {"id": 3, "name": str(body["name"])})

        return MockResponse(404, {"error": "no route"})


def MockService(scenario: str = "conformant", host: str = "127.0.0.1", port: int = 0) -> Service:
    return Service(PetstoreBehaviour(scenario), host=host, port=port)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="aprobe 评估基准：Petstore")
    parser.add_argument("--scenario", choices=SCENARIOS, default="conformant")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_forever(PetstoreBehaviour(args.scenario), args.port, "Petstore 基准")


if __name__ == "__main__":
    main()
