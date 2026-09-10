"""OpenAI-compatible 模型端点的本地替身。

用途：让 Agent 模式能在**不接触任何外部服务**的前提下被端到端验证。
它与 `mock_service.py` 是两件事：那个是**被测目标**，这个是**模型端点**。
把两者混在一起就违反了 ADR-0001 的边界，所以它们必须是两个进程/两个模块。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any


class ModelService:
    """按脚本回放 chat/completions 响应，并记录收到的请求。"""

    def __init__(self, script: list[dict[str, Any]], host: str = "127.0.0.1", port: int = 0) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._thread: Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def next_response(self) -> dict[str, Any]:
        if not self.script:
            message: dict[str, Any] = {"role": "assistant", "content": "脚本已用尽"}
        else:
            message = self.script.pop(0)
        tool_calls = message.get("tool_calls") or []
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                                },
                            }
                            for call in tool_calls
                        ],
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    service.requests.append(json.loads(raw.decode("utf-8")))
                except json.JSONDecodeError:
                    service.requests.append({})
                payload = json.dumps(service.next_response()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        return Handler

    def start(self) -> "ModelService":
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ModelService":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
