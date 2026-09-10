"""评估基准 #2：契约边界（每个接口各注入一种不同的违约）。

存在的理由：第一个基准只有一处违约，指标太容易满分。
这里每个接口注入**不同种类**的偏差——缺必填、多出字段、枚举越界、嵌套类型错、
数值形状错、业务规则不成立、状态码错、重定向类型错、响应不是 JSON——
因此任何一个探测能力的缺失都会在指标上单独现形。

场景版本化：同一版本必须给出完全相同的响应，否则指标不可比。
"""

from __future__ import annotations

from typing import Any

from http_kit import MockResponse, Service, run_forever

SCENARIO_VERSION = "2.0.0"
SCENARIOS = ("conformant", "violating")


class EdgeBehaviour:
    def __init__(self, scenario: str = "conformant") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景: {scenario}")
        self.scenario = scenario
        self.version = SCENARIO_VERSION

    @property
    def violating(self) -> bool:
        return self.scenario == "violating"

    def respond(
        self, method: str, path: str, headers: dict[str, str], body: Any | None
    ) -> MockResponse:
        if method != "GET":
            return MockResponse(404, {"error": "no route"})
        parts = [segment for segment in path.split("/") if segment]
        if parts[:1] != ["contracts"] or len(parts) != 2:
            return MockResponse(404, {"error": "no route"})

        return {
            "required": self._required,
            "enum": self._enum,
            "nested": self._nested,
            "numbers": self._numbers,
            "pagination": self._pagination,
            "empty": self._empty,
            "no-content": self._no_content,
            "redirect": self._redirect,
            "broken-json": self._broken_json,
        }.get(parts[1], lambda: MockResponse(404, {"error": "no route"}))()

    # conformant 全部符合契约；violating 每个接口注入一种不同的偏差

    def _required(self) -> MockResponse:
        if self.violating:
            # 缺必填 name，且多出契约禁止的字段
            return MockResponse(200, {"id": 1, "extra": True})
        return MockResponse(200, {"id": 1, "name": "alpha"})

    def _enum(self) -> MockResponse:
        if self.violating:
            return MockResponse(200, {"status": "unknown"})
        return MockResponse(200, {"status": "active"})

    def _nested(self) -> MockResponse:
        if self.violating:
            return MockResponse(200, {"owner": {"email": 42, "tags": ["a", 1]}})
        return MockResponse(200, {"owner": {"email": "owner@example.com", "tags": ["a", "b"]}})

    def _numbers(self) -> MockResponse:
        if self.violating:
            return MockResponse(200, {"ratio": "0.5", "count": 2.5, "flag": "yes"})
        return MockResponse(200, {"ratio": 0.5, "count": 2, "flag": True})

    def _pagination(self) -> MockResponse:
        if self.violating:
            # 结构合法，但 total 与 items 条数不一致：只有业务规则能发现
            return MockResponse(200, {"items": [{"id": 1}], "total": 3})
        return MockResponse(200, {"items": [{"id": 1}, {"id": 2}], "total": 2})

    def _empty(self) -> MockResponse:
        if self.violating:
            return MockResponse(200, {"items": [{"id": 1}], "total": 0})
        return MockResponse(200, {"items": [], "total": 0})

    def _no_content(self) -> MockResponse:
        if self.violating:
            return MockResponse(200, {"unexpected": "body"})
        return MockResponse(204, raw=b"")

    def _redirect(self) -> MockResponse:
        status = 301 if self.violating else 302
        return MockResponse(status, {"next": "/contracts/required"}, headers={"Location": "/contracts/required"})

    def _broken_json(self) -> MockResponse:
        if self.violating:
            # 声明是 application/json，实际不是：verify 的能力边界就在这里
            return MockResponse(200, raw=b"not json at all")
        return MockResponse(200, {"status": "active"})


def MockService(scenario: str = "conformant", host: str = "127.0.0.1", port: int = 0) -> Service:
    return Service(EdgeBehaviour(scenario), host=host, port=port)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="aprobe 评估基准：契约边界")
    parser.add_argument("--scenario", choices=SCENARIOS, default="conformant")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    run_forever(EdgeBehaviour(args.scenario), args.port, "契约边界基准")


if __name__ == "__main__":
    main()
