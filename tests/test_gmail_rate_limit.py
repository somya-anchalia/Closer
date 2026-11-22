"""A read refused by Gmail's per-minute quota is retried; other errors and sends aren't."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import closer.integrations.gmail as gmail

QUOTA = '{"error":{"code":403,"message":"Quota exceeded for quota metric \'Total Query Cost\'"}}'


def provider(monkeypatch, replies):
    client = httpx.AsyncClient
    calls = []

    def handle(request):
        calls.append(request)
        status, body = replies[min(len(calls), len(replies)) - 1]
        return httpx.Response(status, text=body)
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(gmail.httpx, "AsyncClient",
                        lambda **kw: client(transport=transport, **kw))
    monkeypatch.setattr(gmail, "RATE_LIMIT_WAITS", (0, 0))
    p = gmail.GmailProvider(SimpleNamespace(persona=SimpleNamespace(
        name="Synthetic", email="sender@example.invalid")), SimpleNamespace())
    p._token = AsyncMock(return_value="synthetic-token")
    return p, calls


@pytest.mark.asyncio
async def test_rate_limited_read_is_retried(monkeypatch):
    p, calls = provider(monkeypatch, [(403, QUOTA), (200, '{"id":"m1"}')])
    assert await p._request("GET", "/messages/m1") == {"id": "m1"}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_rate_limit_gives_up_after_its_retries(monkeypatch):
    p, calls = provider(monkeypatch, [(429, "{}")])
    assert await p._request("GET", "/messages/m1") is None
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_other_403_is_not_retried(monkeypatch):
    p, calls = provider(monkeypatch, [(403, '{"error":{"message":"insufficient permissions"}}')])
    assert await p._request("GET", "/messages/m1") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_is_never_retried(monkeypatch):
    p, calls = provider(monkeypatch, [(429, "{}"), (200, '{"id":"x"}')])
    assert await p._request("POST", "/messages/send", sending=True, json={}) is None
    assert len(calls) == 1
