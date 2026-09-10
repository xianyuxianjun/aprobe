"""版本化的 Mock 被测目标（评估基准）。

边界：它是计量基准，不是能力上限——它让指标可复现，但不代表 aprobe 只能测它。
场景是版本化的：同一场景版本必须给出完全相同的响应，否则指标不可比。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

SCENARIO_VERSION = "1.0.0"

_PETS = {1: "Ada", 2: "Bao"}


def _pet(pid: int) -> dict[str, object]:
    return {
        "id": pid,
        "name": _PETS[pid],
        "owner": {"email": f"owner{pid}@example.com", "token": "mock-secret-token"},
    }


@dataclass(frozen=True)
class MockResponse:
    status: int
    body: dict[str, object]


class MockBehaviour:
    """场景决定行为。conformant 遵守契约；violating 故意违反一处契约。"""

    def __init__(self, scenario: str = "conformant") -> None:
        if scenario not in ("conformant", "violating"):
            raise ValueError(f"未知场景: {scenario}")
        self.scenario = scenario

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: object | None
    ) -> MockResponse:
        parts = [segment for segment in path.split("/") if segment]
        authorized = "authorization" in {key.lower() for key in headers}

        if method == "GET" and parts == ["__scenario"]:
            # 评估基准的自述身份：让“量错了基准”变成一条可检的错误，而不是一个静默偏差
            return MockResponse(200, {"scenario": self.scenario, "version": SCENARIO_VERSION})

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


def _handler_for(behaviour: MockBehaviour) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body: object | None = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = None
            response = behaviour.respond(method, parsed.path, dict(self.headers), body)
            payload = json.dumps(response.body).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # 保持输出干净
            return

    return Handler


class MockService:
    """可在测试或本地演示中启动的 Mock 服务。端口 0 表示自动分配。"""

    def __init__(self, scenario: str = "conformant", host: str = "127.0.0.1", port: int = 0) -> None:
        self.behaviour = MockBehaviour(scenario)
        self._server = ThreadingHTTPServer((host, port), _handler_for(self.behaviour))
        self._thread: Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> "MockService":
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockService":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="aprobe 评估基准 Mock 服务")
    parser.add_argument("--scenario", choices=("conformant", "violating"), default="conformant")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = MockService(args.scenario, port=args.port)
    print(f"Mock 场景 {args.scenario}（基准版本 {SCENARIO_VERSION}）监听 {service.base_url}")
    try:
        service.start()
        while True:
            import time

            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
