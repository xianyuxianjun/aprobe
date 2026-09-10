"""aprobe：面向 OpenAPI 的、有界自主的接口契约测试 Agent。"""

from .models import (
    APROBE_VERSION,
    Assertion,
    AssertionKind,
    AssertionResult,
    Observation,
    Operation,
    TestCase,
    TestRun,
    Verdict,
)

__all__ = [
    "APROBE_VERSION",
    "Assertion",
    "AssertionKind",
    "AssertionResult",
    "Observation",
    "Operation",
    "TestCase",
    "TestRun",
    "Verdict",
]

__version__ = APROBE_VERSION
