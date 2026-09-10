"""两个评估基准共用的 HTTP 管道。

基准之间的差别只在路由与场景，不在管道。把管道抽出来的理由与 AgentLoop 相同：
机制与意图分开，新增一个基准不需要再抄一遍 handler。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class MockResponse:
    status: int
    body: Any = None
    raw: bytes | None = None
    content_type: str = "application/json"
    headers: dict[str, str] | None = None

    def payload(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return json.dumps(self.body).encode("utf-8")


class Behaviour(Protocol):
    """一个基准的全部行为：给定请求，返回响应。"""

    scenario: str
    version: str

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: Any | None
    ) -> MockResponse: ...


def _handler_for(behaviour: Behaviour) -> type[BaseHTTPRequestHandler]:
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
            body: Any | None = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = None

            if parsed.path == "/__scenario":
                # 基准的自述身份：让“量错了基准”变成一条可检的错误，而不是一个静默偏差
                response = MockResponse(200, {"scenario": behaviour.scenario, "version": behaviour.version})
            else:
                response = behaviour.respond(method, parsed.path, dict(self.headers), body)

            payload = response.payload()
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            for name, value in (response.headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # 保持输出干净
            return

    return Handler


class Service:
    """可在测试或本地演示中启动的基准服务。端口 0 表示自动分配。"""

    def __init__(self, behaviour: Behaviour, host: str = "127.0.0.1", port: int = 0) -> None:
        self.behaviour = behaviour
        self._server = ThreadingHTTPServer((host, port), _handler_for(behaviour))
        self._thread: Thread | None = None

    @property
    def scenario(self) -> str:
        return self.behaviour.scenario

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> "Service":
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "Service":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def run_forever(behaviour: Behaviour, port: int, banner: str) -> None:
    import time

    service = Service(behaviour, port=port)
    print(f"{banner} 监听 {service.base_url}（场景 {behaviour.scenario}，版本 {behaviour.version}）")
    try:
        service.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()
