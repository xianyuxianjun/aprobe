"""被测目标策略。

这是"能不能发请求、用什么方法、访问哪个主机"的确定性防线。
模型与用户参数都只能被它检查，不能绕过它。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(True, "ok")

    @classmethod
    def deny(cls, reason: str) -> "PolicyDecision":
        return cls(False, reason)


def _host_allowed(host: str, allow: tuple[str, ...]) -> bool:
    host = host.lower()
    for entry in allow:
        candidate = entry.strip().lower()
        if not candidate:
            continue
        if candidate.startswith("*."):
            suffix = candidate[1:]
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return True
        elif host == candidate:
            return True
    return False


class TargetPolicy:
    """一次请求被允许之前必须通过的全部检查。"""

    def __init__(
        self,
        allow: list[str],
        read_only: bool = True,
        allow_write: bool = False,
        timeout_ms: int = 5000,
        max_response_bytes: int = 65536,
        retries: int = 0,
    ) -> None:
        self.allow = tuple(allow)
        self.read_only = read_only
        self.allow_write = allow_write
        self.timeout_ms = timeout_ms
        self.max_response_bytes = max_response_bytes
        self.retries = retries

    def check(self, *, url: str, method: str) -> PolicyDecision:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return PolicyDecision.deny(f"只允许 http/https，收到 {parts.scheme or '(空)'}")
        if not parts.hostname:
            return PolicyDecision.deny("URL 缺少主机名")
        if parts.username or parts.password:
            return PolicyDecision.deny("URL 不允许内嵌凭据")
        if not self.allow:
            return PolicyDecision.deny("允许范围为空，默认拒绝一切目标")
        if not _host_allowed(parts.hostname, self.allow):
            return PolicyDecision.deny(f"主机 {parts.hostname} 不在允许范围内")

        verb = method.upper()
        if verb not in READ_ONLY_METHODS and not self.allow_write:
            return PolicyDecision.deny(f"{verb} 不是只读方法，且未显式开启写操作")
        return PolicyDecision.allow()

    def check_case(self, *, url: str, method: str, is_write: bool) -> PolicyDecision:
        """用例级检查：写用例必须既声明 write=true，又得到全局开关允许。"""
        decision = self.check(url=url, method=method)
        if not decision.allowed:
            return decision
        verb = method.upper()
        if is_write and verb in READ_ONLY_METHODS:
            return PolicyDecision.deny(f"用例声明为写操作，但 {verb} 是只读方法，声明与请求不一致")
        if is_write and not self.allow_write:
            return PolicyDecision.deny("用例声明为写操作，但未显式开启写操作")
        if not is_write and verb not in READ_ONLY_METHODS:
            return PolicyDecision.deny(f"{verb} 属于写方法，用例必须声明 write=true")
        return PolicyDecision.allow()
