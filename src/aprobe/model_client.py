"""模型客户端的端口与适配器。

边界：这是**唯一**能访问模型端点的模块。它拿不到被测目标地址，也从不构造
面向被测目标的请求——ADR-0001 说的"生成阶段没有网络出口"，指的正是没有
通往被测目标的出口；模型端点是另一条、作用域完全不同的出口。

两个 adapter 让这条 seam 成为真的：
- `OpenAICompatibleClient`：生产用，只走 OpenAI-compatible 的 chat/completions；
- `ScriptedModelClient`：测试用，按脚本回放，不联网。测试因此不依赖外部服务。
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .errors import AprobeError

DEFAULT_TIMEOUT_MS = 60_000


class ModelError(AprobeError):
    """模型端点的调用失败。它不是测试结论，也不允许被当成结论。"""


class ModelToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    parse_error: str = ""


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


class ModelClient(Protocol):
    name: str

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        """一次模型请求。实现方必须把用量放进 ModelReply，否则预算无法统计。"""


class ScriptedModelClient:
    """按固定脚本回放回复。测试用的 adapter，不联网。"""

    def __init__(self, replies: list[ModelReply], name: str = "scripted") -> None:
        self._replies = list(replies)
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls.append({"system": system, "messages": [dict(item) for item in messages], "tools": tools})
        if not self._replies:
            raise ModelError("脚本已用尽：模型被要求多走一步，但测试没有准备对应回复")
        return self._replies.pop(0)


class OpenAICompatibleClient:
    """OpenAI-compatible 的 chat/completions 客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = model
        self.timeout_ms = timeout_ms

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout_ms / 1000,
                trust_env=False,  # 与 TestRunner 同理：不信任系统或环境代理
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise ModelError(f"模型端点调用失败: {type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            raise ModelError(f"模型端点返回的不是 JSON: {exc}") from exc

        return _parse_reply(data)


def _parse_reply(data: dict[str, Any]) -> ModelReply:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(f"模型端点没有返回 choices: {str(data)[:200]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ModelError("模型端点返回的 choices[0] 缺少 message")

    calls: list[ModelToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        raw_arguments = function.get("arguments")
        arguments: dict[str, Any] = {}
        parse_error = ""
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            import json

            try:
                parsed = json.loads(raw_arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    parse_error = "工具参数不是 JSON 对象"
            except json.JSONDecodeError as exc:
                parse_error = f"工具参数不是合法 JSON: {exc}"
        calls.append(
            ModelToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
                parse_error=parse_error,
            )
        )

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return ModelReply(
        text=str(message.get("content") or ""),
        tool_calls=calls,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        stop_reason=str(choices[0].get("finish_reason") or ""),
    )


def from_environment(environ: dict[str, str]) -> ModelClient | None:
    """按环境变量装配客户端。没有配置就返回 None——降级模式会接手（ADR-0003）。"""
    base_url = environ.get("APROBE_MODEL_BASE_URL")
    model = environ.get("APROBE_MODEL")
    if not base_url or not model:
        return None
    return OpenAICompatibleClient(
        base_url=base_url,
        api_key=environ.get("APROBE_MODEL_API_KEY", ""),
        model=model,
        timeout_ms=int(environ.get("APROBE_MODEL_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS),
    )
