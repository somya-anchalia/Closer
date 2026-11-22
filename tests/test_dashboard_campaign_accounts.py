"""The Campaigns tab with several mailboxes: accounts first, then each
account's own people, campaigns and sign-off."""

import asyncio
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import closer.config as closer_config
import closer.dashboard as dash
import closer.integrations.mail_provider as mail_provider
from closer.config import EmailChannelConfig, MailboxConfig, WarmupConfig
from closer.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
ANA = "ana@sender.example.com"
BEN = "ben@sender.example.com"


def _config(mailboxes=True):
    started = (date.today() - timedelta(days=20)).isoformat()
    warming = (date.today() - timedelta(days=4)).isoformat()
    boxes = [
        MailboxConfig(email=ANA, name="Ana", role="Founder", warmup_start=started),
        MailboxConfig(email=BEN, name="Ben", role="Sales", warmup_start=warming),
    ] if mailboxes else []
    email = EmailChannelConfig(provider="gmail", max_daily_sends=15, mailboxes=boxes,
                               warmup=WarmupConfig(start_date=started))
    return SimpleNamespace(
        persona=SimpleNamespace(name="Ana", role="Founder", company="Sender Co", email=ANA),
        channels=SimpleNamespace(email=email))


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "closer.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "closer.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "closer.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setattr(dash, "_closer_can_send", lambda: True)
    monkeypatch.setattr(closer_config, "load_config", _config)
    monkeypatch.setattr(closer_config, "load_env", lambda: SimpleNamespace())
    signed_in = SimpleNamespace(is_configured=lambda: True)
    signed_out = SimpleNamespace(is_configured=lambda: False)
    monkeypatch.setattr(mail_provider, "get_mail_providers",
                        lambda config, env: {ANA: signed_in, BEN: signed_out})
    asyncio.run(StateManager(db_path=str(db)).init_db())
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _sql(env, query, params=()):
    with sqlite3.connect(env["db"]) as db:
        db.execute(query, params)


def _prospect(env, pid, sender=""):
    _sql(env, "INSERT INTO prospects (id, first_name, last_name, email, email_status, status, "
              "company, sender_email) VALUES (?, '', '', ?, 'verified', 'new', 'Acme', ?)",
         (pid, f"{pid}@prospect.example.com", sender))


def _campaign(env, cid, prospect_ids, body="Hi there.\n\nAna\nFounder\nSender Co"):
    _sql(env, "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
              "status) VALUES (?, ?, 'email', ?, ?, 'draft')",
         (cid, cid, json.dumps([{"step": 1, "subject": "hi", "body": body, "delay_days": 0}]),
          json.dumps(prospect_ids)))


def _outbox(env, oid, pid, status, from_email="", sent_at=None):
    _sql(env, "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, subject, "
              "body, status, send_at, sent_at, from_email) "
              "VALUES (?, ?, ?, 1, 'sequence', 'x@prospect.example.com', 's', 'b', ?, "
              "'2026-01-01T00:00:00', ?, ?)",
         (oid, oid, pid, status, sent_at, from_email))


def _by_email(client):
    body = client.get("/api/campaigns/accounts").json()
    assert body["multi"] is True
    return {a["email"]: a for a in body["accounts"]}


def test_one_mailbox_has_no_accounts_to_choose(client, monkeypatch, env):
    monkeypatch.setattr(closer_config, "load_config", lambda: _config(mailboxes=False))
    _prospect(env, "p1")
    _campaign(env, "c1", ["p1"])
    assert client.get("/api/campaigns/accounts").json() == {"multi": False, "accounts": []}
    assert [c["id"] for c in client.get("/api/campaigns").json()] == ["c1"]


def test_accounts_count_their_own_people_and_campaigns(client, env):
    _prospect(env, "p1", ANA)
    _prospect(env, "p2", BEN)
    _prospect(env, "p3", BEN)
    _campaign(env, "c1", ["p1", "p2"])
    _campaign(env, "c2", ["p3"])
    accounts = _by_email(client)
    assert (accounts[ANA]["campaigns"], accounts[ANA]["people"]) == (1, 1)
    assert (accounts[BEN]["campaigns"], accounts[BEN]["people"]) == (2, 2)
    assert "unassigned" not in accounts


def test_accounts_show_sign_in_and_warmup(client, env):
    accounts = _by_email(client)
    assert accounts[ANA]["signed_in"] is True and accounts[ANA]["warm_from"] == ""
    assert accounts[ANA]["cold_cap_today"] > 0
    assert accounts[BEN]["signed_in"] is False
    assert accounts[BEN]["warm_from"] == (date.today() + timedelta(days=10)).isoformat()
    assert accounts[BEN]["cold_cap_today"] == 0


def test_queue_counts_per_account_and_blank_rows_are_the_primarys(client, env):
    _prospect(env, "p1", ANA)
    _outbox(env, "o1", "p1", "approved", "")
    _outbox(env, "o2", "p1", "approved", BEN)
    _outbox(env, "o3", "p1", "pending_review", BEN)
    _outbox(env, "o4", "p1", "sent", BEN, sent_at=date.today().isoformat() + "T01:00:00")
    _outbox(env, "o5", "p1", "sent", BEN, sent_at="2020-01-01T01:00:00")
    accounts = _by_email(client)
    assert accounts[ANA]["approved"] == 1
    assert (accounts[BEN]["approved"], accounts[BEN]["to_review"], accounts[BEN]["sent_today"]) == (1, 1, 1)


def test_opening_an_account_shows_only_its_people_and_its_sign_off(client, env):
    _prospect(env, "p1", ANA)
    _prospect(env, "p2", BEN)
    _campaign(env, "c1", ["p1", "p2"])
    _campaign(env, "c2", ["p1"])
    rows = client.get("/api/campaigns", params={"mailbox": BEN}).json()
    assert [c["id"] for c in rows] == ["c1"]
    assert [r["id"] for r in rows[0]["recipients"]] == ["p2"]
    assert rows[0]["prospect_ids"] == ["p2"]
    body = rows[0]["sequence"][0]["body"]
    assert body.endswith("Ben\nSales\nSender Co") and "Ana" not in body


def test_no_filter_still_lists_everything(client, env):
    _prospect(env, "p1", ANA)
    _prospect(env, "p2", BEN)
    _campaign(env, "c1", ["p1", "p2"])
    rows = client.get("/api/campaigns").json()
    assert len(rows[0]["recipients"]) == 2
    assert rows[0]["sequence"][0]["body"].endswith("Ana\nFounder\nSender Co")


def test_people_without_a_listed_account_stay_visible(client, env):
    _prospect(env, "p1", "")
    _prospect(env, "p2", "removed@sender.example.com")
    _campaign(env, "c1", ["p1", "p2"])
    accounts = _by_email(client)
    assert accounts[dash.UNASSIGNED_ACCOUNT]["unassigned"] is True
    assert accounts[dash.UNASSIGNED_ACCOUNT]["people"] == 2
    rows = client.get("/api/campaigns", params={"mailbox": dash.UNASSIGNED_ACCOUNT}).json()
    assert {r["id"] for r in rows[0]["recipients"]} == {"p1", "p2"}
    assert rows[0]["sequence"][0]["body"].endswith("Ana\nFounder\nSender Co")


def test_unknown_account_shows_nothing(client, env):
    _prospect(env, "p1", ANA)
    _campaign(env, "c1", ["p1"])
    assert client.get("/api/campaigns", params={"mailbox": "nobody@example.com"}).json() == []


def test_hand_sending_opens_gmail_as_the_account(client, env, monkeypatch):
    monkeypatch.setattr(dash, "_closer_can_send", lambda: False)
    _prospect(env, "p1", BEN)
    _campaign(env, "c1", ["p1"])
    rows = client.get("/api/campaigns", params={"mailbox": BEN}).json()
    assert rows[0]["send_as"] == BEN
