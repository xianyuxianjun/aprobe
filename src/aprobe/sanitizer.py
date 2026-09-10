"""证据脱敏。

边界：明文凭据不落库、不写日志、不进报告。脱敏发生在写入之前，
而不是在展示之前——否则 Trace 里就留下了不该留的东西。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
    }
)

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[-_]?key|authorization|cookie|credential|private[-_]?key|session[-_]?id)",
    re.IGNORECASE,
)


def sanitize_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): REDACTED if str(key).lower() in SENSITIVE_HEADERS else str(value)
        for key, value in headers.items()
    }


def sanitize_value(value: Any) -> Any:
    """递归脱敏 JSON 结构。命中敏感键名的值整体替换。"""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def redact_url(url: str) -> str:
    """去掉 URL 中的 userinfo，并把敏感查询参数的值替换掉。"""
    parts = urlsplit(url)
    query = parts.query
    if query:
        pairs = []
        for chunk in query.split("&"):
            name, _, _ = chunk.partition("=")
            pairs.append(f"{name}={REDACTED}" if _SENSITIVE_KEY.search(name) else chunk)
        query = "&".join(pairs)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


_TEXT_PATTERNS = (
    re.compile(r'("(?:[^"]*(?:password|secret|token|api[-_]?key|authorization)[^"]*)"\s*:\s*)"[^"]*"', re.IGNORECASE),
    re.compile(r"\b((?:password|secret|token|api[-_]?key|access[-_]?token)\s*=\s*)[^&\s;\"']+", re.IGNORECASE),
)


def sanitize_text(text: str) -> str:
    """针对非 JSON 响应体的兜底脱敏，避免密钥以字符串形式落库。"""
    for pattern in _TEXT_PATTERNS:
        text = pattern.sub(lambda match: f'{match.group(1)}"{REDACTED}"' if match.group(1).rstrip().endswith(":") else f"{match.group(1)}{REDACTED}", text)
    return text


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字节上限截断，返回 (文本, 是否被截断)。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore") + "…[TRUNCATED]", True
