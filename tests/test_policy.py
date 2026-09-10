from __future__ import annotations

from aprobe.policy import TargetPolicy


def test_empty_allowlist_denies_everything() -> None:
    decision = TargetPolicy(allow=[]).check(url="http://127.0.0.1:8000/pets", method="GET")
    assert not decision.allowed
    assert "允许范围为空" in decision.reason


def test_host_outside_allowlist_is_denied() -> None:
    decision = TargetPolicy(allow=["127.0.0.1"]).check(url="http://example.com/pets", method="GET")
    assert not decision.allowed
    assert "不在允许范围" in decision.reason


def test_allowlist_supports_wildcard_subdomain() -> None:
    policy = TargetPolicy(allow=["*.internal.test"])
    assert policy.check(url="http://api.internal.test/x", method="GET").allowed
    assert not policy.check(url="http://internal.test/x", method="GET").allowed


def test_non_http_scheme_and_userinfo_are_denied() -> None:
    policy = TargetPolicy(allow=["127.0.0.1"])
    assert not policy.check(url="ftp://127.0.0.1/x", method="GET").allowed
    assert not policy.check(url="http://user:pass@127.0.0.1/x", method="GET").allowed


def test_write_methods_need_explicit_flag() -> None:
    strict = TargetPolicy(allow=["127.0.0.1"])
    assert not strict.check(url="http://127.0.0.1/pets", method="POST").allowed
    permissive = TargetPolicy(allow=["127.0.0.1"], allow_write=True)
    assert permissive.check(url="http://127.0.0.1/pets", method="POST").allowed


def test_case_level_check_requires_declaration_to_match_method() -> None:
    policy = TargetPolicy(allow=["127.0.0.1"], allow_write=True)
    mismatch = policy.check_case(url="http://127.0.0.1/pets", method="GET", is_write=True)
    assert not mismatch.allowed
    assert "只读方法" in mismatch.reason

    undeclared = policy.check_case(url="http://127.0.0.1/pets", method="POST", is_write=False)
    assert not undeclared.allowed

    ok = policy.check_case(url="http://127.0.0.1/pets", method="POST", is_write=True)
    assert ok.allowed


def test_write_declared_but_globally_disabled() -> None:
    policy = TargetPolicy(allow=["127.0.0.1"], allow_write=False)
    decision = policy.check_case(url="http://127.0.0.1/pets", method="POST", is_write=True)
    assert not decision.allowed
    assert "未显式开启写操作" in decision.reason
