"""评估基准 #1：Petstore（契约结构与业务规则的正常/违约对照）。

边界：它是计量基准，不是能力上限——它让指标可复现，但不代表 aprobe 只能测它。
场景是版本化的：同一场景版本必须给出完全相同的响应，否则指标不可比。
"""

from __future__ import annotations

from typing import Any

from http_kit import MockResponse, Service, run_forever

SCENARIO_VERSION = "1.1.0"
SCENARIOS = ("conformant", "violating")

_PETS = {1: "Ada", 2: "Bao"}
#: 探针槽：POST 写进这个固定 id，于是"创建→读取"能闭环，而响应仍可复现——
#: 真实 API 会为新资源分配新 id，那种行为在基准里无法保证同一场景版本给出相同响应。
PROBE_SLOT = 1


class PetstoreBehaviour:
    def __init__(self, scenario: str = "conformant") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景: {scenario}")
        self.scenario = scenario
        self.version = SCENARIO_VERSION
        #: 探针槽被写入的名字；未写入时为 None
        self._probe_name: str | None = None

    def _pet(self, pid: int) -> dict[str, object]:
        name = self._probe_name if pid == PROBE_SLOT and self._probe_name else _PETS[pid]
        return {
            "id": pid,
            "name": name,
            "owner": {"email": f"owner{pid}@example.com", "token": "mock-secret-token"},
        }

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: Any | None
    ) -> MockResponse:
        parts = [segment for segment in path.split("/") if segment]
        authorized = "authorization" in {key.lower() for key in headers}

        if method == "GET" and parts == ["health"]:
            return MockResponse(200, {"status": "ok"})

        if method == "GET" and parts == ["pets"]:
            return MockResponse(200, {"items": [self._pet(pid) for pid in sorted(_PETS)], "total": len(_PETS)})

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
            return MockResponse(200, self._pet(int(parts[1])))

        if method == "POST" and parts == ["pets"]:
            if not isinstance(body, dict) or not body.get("name"):
                return MockResponse(422, {"error": "name is required"})
            # 写进探针槽：于是随后 GET /pets/{PROBE_SLOT} 能读回刚写的名字，
            # 而同一场景版本仍然给出完全相同的响应（请求体来自用例文件，是固定的）
            self._probe_name = str(body["name"])
            return MockResponse(201, {"id": PROBE_SLOT, "name": self._probe_name})

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
