"""The free check_if_email_exists first pass (fictional addresses only)."""

import pytest

from closer.integrations import email_finder as ef


def _fake_tool(monkeypatch, verdict):
    async def fake(email):
        return None if verdict is None else {"status": verdict, "catch_all": False}
    monkeypatch.setattr(ef, "verify_ceie", fake)


def _truemail(monkeypatch, checks):
    async def fake(email):
        return checks
    monkeypatch.setattr(ef, "verify_truemail", fake)


def _paid(monkeypatch, result):
    async def fake(email, mx_type, env):
        return result
    monkeypatch.setattr(ef, "_paid_verdict", fake)


@pytest.mark.asyncio
async def test_invalid_is_dropped_without_paid_check(monkeypatch):
    _fake_tool(monkeypatch, "invalid")
    _paid(monkeypatch, ("verified", False))  # must not be reached
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("invalid", False)


@pytest.mark.asyncio
async def test_safe_alone_is_only_risky(monkeypatch):
    # Free-tool "safe" addresses bounced: without a paid verdict it stays risky.
    _fake_tool(monkeypatch, "safe")
    _truemail(monkeypatch, {"mx": True, "mx_blacklist": True, "smtp": True})
    _paid(monkeypatch, (None, False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("risky", False)


@pytest.mark.asyncio
async def test_safe_still_goes_to_the_paid_verifier(monkeypatch):
    _fake_tool(monkeypatch, "safe")
    _truemail(monkeypatch, None)
    _paid(monkeypatch, ("invalid", False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("invalid", False)
    _paid(monkeypatch, ("verified", False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("verified", False)


@pytest.mark.asyncio
async def test_risky_stays_risky_when_paid_is_inconclusive(monkeypatch):
    _fake_tool(monkeypatch, "risky")
    _truemail(monkeypatch, None)
    _paid(monkeypatch, (None, False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("risky", False)


@pytest.mark.asyncio
async def test_paid_verdict_overrides_risky(monkeypatch):
    _fake_tool(monkeypatch, "risky")
    _truemail(monkeypatch, None)
    _paid(monkeypatch, ("verified", False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("verified", False)


@pytest.mark.asyncio
async def test_unknown_falls_through_to_paid(monkeypatch):
    _fake_tool(monkeypatch, None)
    _paid(monkeypatch, (None, False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == (None, False)


@pytest.mark.asyncio
async def test_tool_missing_or_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ef.shutil, "which", lambda name: None)
    monkeypatch.setenv("CLOSER_EMAIL_PRECHECK", "1")
    assert await ef.verify_ceie("a@example.com") is None
    monkeypatch.setenv("CLOSER_EMAIL_PRECHECK", "0")
    assert await ef.verify_ceie("a@example.com") is None


@pytest.mark.asyncio
async def test_risky_with_all_truemail_checks_stays_risky(monkeypatch):
    _fake_tool(monkeypatch, "risky")
    _truemail(monkeypatch, {"mx": True, "mx_blacklist": True, "smtp": True})
    _paid(monkeypatch, (None, False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("risky", False)


@pytest.mark.asyncio
async def test_risky_with_smtp_error_stays_risky(monkeypatch):
    _fake_tool(monkeypatch, "risky")
    _truemail(monkeypatch, {"mx": True, "mx_blacklist": True, "smtp": False})
    _paid(monkeypatch, (None, False))
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("risky", False)


@pytest.mark.asyncio
async def test_risky_with_dead_domain_is_invalid(monkeypatch):
    _fake_tool(monkeypatch, "risky")
    _truemail(monkeypatch, {"mx": False, "mx_blacklist": False, "smtp": False})
    _paid(monkeypatch, ("verified", False))  # must not be reached
    assert await ef._verify_candidate("a@example.com", "microsoft", None) == ("invalid", False)


@pytest.mark.asyncio
async def test_truemail_missing_or_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ef.shutil, "which", lambda name: None)
    monkeypatch.setenv("CLOSER_EMAIL_PRECHECK", "1")
    assert await ef.verify_truemail("a@example.com") is None
    monkeypatch.setenv("CLOSER_EMAIL_PRECHECK", "0")
    assert await ef.verify_truemail("a@example.com") is None


@pytest.mark.asyncio
async def test_run_tool_returns_output_and_enforces_its_own_timeout():
    import subprocess
    assert await ef._run_tool(["/bin/echo", "ok"], timeout=5) == b"ok\n"
    with pytest.raises(subprocess.TimeoutExpired):
        await ef._run_tool(["/bin/sleep", "5"], timeout=0.3)


@pytest.mark.asyncio
async def test_tools_do_not_inherit_closers_smtp_settings(monkeypatch):
    # An SMTP mailbox setup loads SMTP_PORT=587 from .env; the verifier CLI
    # reads the same variable and would probe the wrong port.
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("PROXY_HOST", "proxy.example.invalid")
    out = await ef._run_tool(["/usr/bin/env"], timeout=5)
    assert b"SMTP_PORT=" not in out and b"PROXY_HOST=" not in out
    assert b"PATH=" in out


@pytest.mark.asyncio
async def test_verifier_allowlist_spends_only_the_named_one(monkeypatch):
    calls = []

    async def fake(name):
        async def verdict(email, key):
            calls.append(name)
            return None
        return verdict

    monkeypatch.setattr(ef, "verify_reoon", await fake("reoon"))
    monkeypatch.setattr(ef, "verify_zerobounce", await fake("zerobounce"))
    monkeypatch.setattr(ef, "verify_hunter", await fake("hunter"))
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")

    async def no_smtp(email):
        calls.append("smtp")
        return None
    monkeypatch.setattr(ef, "verify_email_smtp", no_smtp)

    class Env:
        reoon_api_key = "k"
        zerobounce_api_key = "k"

    monkeypatch.setenv("CLOSER_VERIFIERS", "reoon")
    for mx_type in ("microsoft", "other"):
        await ef._paid_verdict("a@example.com", mx_type, Env())
    assert calls == ["reoon", "reoon"]

    # The list is also the order: free tiers first, each falling through
    # when it has no answer (out of credits), on every domain type.
    calls.clear()
    monkeypatch.setenv("CLOSER_VERIFIERS", "zerobounce,hunter,reoon")
    for mx_type in ("microsoft", "other"):
        await ef._paid_verdict("a@example.com", mx_type, Env())
    assert calls == ["zerobounce", "hunter", "reoon"] * 2

    calls.clear()
    monkeypatch.delenv("CLOSER_VERIFIERS")
    await ef._paid_verdict("a@example.com", "microsoft", Env())
    assert calls == ["zerobounce", "reoon", "hunter"]
