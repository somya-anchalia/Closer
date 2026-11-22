"""Sending from several mailboxes: settings, the even split, each mailbox's
caps, sign-off and sign-in, and replies going back out the way they came."""

import os
import tempfile
from datetime import date

import pytest
import pytest_asyncio
from pydantic import ValidationError

from closer.config import (
    EmailChannelConfig, MailboxConfig, cold_daily_cap, daily_send_cap, mailbox_for,
    mailboxes_of, primary_mailbox, sender_role,
)
from closer.integrations.gmail import GmailProvider, _load_token, _signed_in_as, token_path_for
from closer.integrations.mail_provider import (
    InboundMessage, MailProvider, SendResult, get_mail_providers,
)
from closer.mailboxes import is_multi, sender_of, sign_off, stored_address, with_sign_off
from closer.models.campaign import Campaign, EmailStep
from closer.models.company import Company
from closer.models.prospect import Prospect
from closer.state import StateManager

ANA = "ana@sender.example.com"
BEN = "ben@sender.example.com"
CAM = "cam@sender.example.com"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self, configured=True, read_error=None):
        self.configured = configured
        self.read_error = read_error
        self.sent = []
        self.inbound = []
        self.confirmed = 0

    def is_configured(self):
        return self.configured

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<{to_email}-{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        if self.read_error:
            raise RuntimeError(self.read_error)
        return self.inbound

    def confirm_read(self):
        self.confirmed += 1

    async def test_connection(self):
        return True, "fake"


def boxes(**caps):
    return [
        MailboxConfig(email=ANA, name="Ana", role="Founder", **caps),
        MailboxConfig(email=BEN, name="Ben", role="Partner", **caps),
        MailboxConfig(email=CAM, name="Cam", **caps),
    ]


def make_config(mailboxes=None, require_approval=True, max_daily=50, warmup=None):
    class Cfg:
        class persona:
            name = "Ana"; email = ANA; company = "Sender Co"; role = "CEO"; tone = "direct"

        class product:
            name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
            objection_responses = {}

        class channels:
            email = EmailChannelConfig(
                provider="gmail", max_daily_sends=max_daily, require_approval=require_approval,
                mailboxes=mailboxes if mailboxes is not None else boxes(),
                **({"warmup": warmup} if warmup else {}))

            class linkedin:
                enabled = False
    return Cfg()


class Env:
    instantly_api_key = ""
    gmail_client_id = "id"
    gmail_client_secret = "secret"


def make_sender(state, config, providers=None):
    from closer.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=config, env=Env())
    sender.providers = providers or {b.email: FakeProvider() for b in mailboxes_of(config)}
    sender.send_pacing = False
    return sender


class StubBrain:
    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.last_prompt = prompt
        return "question" if task == "classify_intent" or "category" in prompt.lower() else "Sure."


def make_handler(state, config, providers):
    from closer.agents.handler import Handler
    handler = Handler(brain=StubBrain(), state=state, config=config, env=Env())
    handler.providers = providers
    return handler


async def seed(state, n=1, company_id="", prefix="p", status="new"):
    ids = []
    for i in range(n):
        ids.append(await state.add_prospect(Prospect(
            first_name="Jo", last_name=f"Doe{i}", title="VP", company="Acme",
            company_id=company_id, email=f"{prefix}{i}@acme.example.com",
            email_status="verified", email_verified=True, status=status,
        )))
    return ids


async def seed_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="test", channel="email",
        sequence=[EmailStep(step=1, subject="hi {{first_name}}",
                            body="Body for {{company}}. Question?", delay_days=0)],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── settings ──


def test_mailbox_needs_an_address_and_a_name():
    with pytest.raises(ValidationError):
        MailboxConfig(email="not-an-address", name="Ana")
    with pytest.raises(ValidationError):
        MailboxConfig(email=ANA, name="  ")
    with pytest.raises(ValidationError):
        MailboxConfig(email=ANA, name="Ana", warmup_start="soon")


def test_mailbox_address_is_normalised_and_dates_read_from_yaml():
    box = MailboxConfig(email=" ANA@Sender.Example.com ", name="Ana", warmup_start=date(2026, 1, 2))
    assert box.email == ANA
    assert box.warmup_start == "2026-01-02"


def test_a_mailbox_listed_twice_is_refused():
    with pytest.raises(ValidationError):
        EmailChannelConfig(mailboxes=[MailboxConfig(email=ANA, name="Ana"),
                                      MailboxConfig(email=ANA, name="Other")])


def test_without_mailboxes_the_persona_is_the_only_one():
    config = make_config(mailboxes=[])
    assert not is_multi(config)
    assert [b.email for b in mailboxes_of(config)] == [ANA]
    assert primary_mailbox(config).name == "Ana"
    assert stored_address(config, primary_mailbox(config)) == ""


def test_primary_is_the_persona_when_listed_else_the_first():
    assert primary_mailbox(make_config(boxes()[::-1])).email == ANA
    assert primary_mailbox(make_config(boxes()[1:])).email == BEN


def test_mailbox_for_finds_blank_and_unknown():
    config = make_config()
    assert mailbox_for(config, BEN.upper()).name == "Ben"
    assert mailbox_for(config, "").email == ANA
    assert mailbox_for(config, "gone@sender.example.com") is None


def test_role_falls_back_to_the_persona():
    config = make_config()
    assert sender_role(config, mailbox_for(config, CAM)) == "CEO"
    assert sender_role(config, mailbox_for(config, BEN)) == "Partner"


def test_each_mailbox_has_its_own_cap_and_ramp():
    email = EmailChannelConfig(max_daily_sends=15, warmup={"start_date": "2026-01-01"})
    today = date(2026, 3, 1)
    assert cold_daily_cap(email, today) == 15
    fresh = MailboxConfig(email=BEN, name="Ben", warmup_start="2026-02-28")
    assert cold_daily_cap(email, today, mailbox=fresh) == 0   # day 1 of warmup
    started = MailboxConfig(email=BEN, name="Ben", warmup_start="2026-02-15")
    assert cold_daily_cap(email, today, mailbox=started) == 5  # day 14: first sending day
    capped = MailboxConfig(email=BEN, name="Ben", max_daily_sends=3)
    assert daily_send_cap(email, capped) == 3
    assert cold_daily_cap(email, today, mailbox=capped) == 3


# ── the even split ──


@pytest.mark.asyncio
async def test_prospects_split_evenly_and_keep_their_mailbox(state):
    ids = await seed(state, 7)
    choices = [ANA, BEN, CAM]
    picked = [await state.assign_sender(i, choices) for i in ids]
    assert picked == [ANA, BEN, CAM, ANA, BEN, CAM, ANA]
    assert await state.assign_sender(ids[1], choices) == BEN
    assert await state.get_prospect_sender(ids[1]) == BEN


@pytest.mark.asyncio
async def test_one_company_hears_from_one_mailbox(state):
    company_id = await state.add_company(Company(name="Acme", domain="acme.example.com"))
    other = await seed(state, 1, prefix="other")
    await state.assign_sender(other[0], [ANA, BEN])  # Ana has one prospect now
    first, second = await seed(state, 2, company_id=company_id)
    assert await state.assign_sender(first, [ANA, BEN]) == BEN
    assert await state.assign_sender(second, [ANA, BEN]) == BEN


@pytest.mark.asyncio
async def test_a_removed_mailbox_is_replaced(state):
    pid = (await seed(state))[0]
    assert await state.assign_sender(pid, [ANA]) == ANA
    assert await state.assign_sender(pid, [BEN]) == BEN


@pytest.mark.asyncio
async def test_no_choices_assigns_nothing(state):
    pid = (await seed(state))[0]
    assert await state.assign_sender(pid, []) == ""
    assert await state.assign_sender("missing", [ANA]) == ""


# ── sign-off ──


def test_sign_off_names_the_person_their_title_and_the_company():
    config = make_config()
    assert sign_off(config, mailbox_for(config, BEN)) == "Ben\nPartner\nSender Co"


def test_sign_off_is_added_once():
    config = make_config()
    ben = mailbox_for(config, BEN)
    once = with_sign_off(config, "Question?", ben)
    assert once == "Question?\n\nBen\nPartner\nSender Co"
    assert with_sign_off(config, once, ben) == once


def test_another_persons_sign_off_is_replaced():
    config = make_config()
    body = "Question?\n\nBest,\nAna\nFounder\nSender Co"
    assert with_sign_off(config, body, mailbox_for(config, BEN)) == "Question?\n\nBen\nPartner\nSender Co"


def test_an_older_title_comma_company_sign_off_is_replaced():
    config = make_config()
    expected = "Question?\n\nBen\nPartner\nSender Co"
    ben = mailbox_for(config, BEN)
    # Includes a title no mailbox holds any more.
    for old in ("Ana\nFounder, Sender Co", "Ben\nPartner, Sender Co", "Ana\nCo-founder, Sender Co"):
        assert with_sign_off(config, f"Question?\n\n{old}", ben) == expected


def test_a_sentence_ending_in_the_company_name_is_not_a_sign_off():
    config = make_config()
    body = "We looked at it, and honestly it could be anyone, Sender Co"
    assert with_sign_off(config, body, mailbox_for(config, BEN)).startswith(body)
    body = "Thanks. Sent from me, Sender Co"
    assert with_sign_off(config, body, mailbox_for(config, BEN)).startswith(body)


def test_a_name_in_the_sentence_is_left_alone():
    config = make_config()
    body = "Ana mentioned your team. Question?"
    assert with_sign_off(config, body, mailbox_for(config, BEN)).startswith(body)


def test_single_mailbox_body_is_untouched():
    config = make_config(mailboxes=[])
    assert with_sign_off(config, "Question?", primary_mailbox(config)) == "Question?"


# ── staging ──


@pytest.mark.asyncio
async def test_staging_records_each_rows_mailbox_and_signs_as_that_person(state):
    config = make_config()
    ids = await seed(state, 3)
    sender = make_sender(state, config)
    await sender._stage_campaign_native(await seed_campaign(state, ids))
    rows = await state.get_outbox(status="pending_review")
    by_prospect = {r["prospect_id"]: r for r in rows}
    assert sorted(r["from_email"] for r in rows) == sorted([ANA, BEN, CAM])
    for pid in ids:
        row = by_prospect[pid]
        box = mailbox_for(config, row["from_email"])
        assert row["from_email"] == await state.get_prospect_sender(pid)
        assert row["body"].endswith(sign_off(config, box))


@pytest.mark.asyncio
async def test_single_mailbox_rows_record_no_mailbox(state):
    config = make_config(mailboxes=[])
    ids = await seed(state, 1)
    sender = make_sender(state, config, providers={})
    sender.provider = FakeProvider()
    await sender._stage_campaign_native(await seed_campaign(state, ids))
    row = (await state.get_outbox(status="pending_review"))[0]
    assert row["from_email"] == ""
    assert row["body"] == "Body for Acme. Question?"


# ── sending ──


async def stage_and_approve(state, config, sender, ids):
    await sender._stage_campaign_native(await seed_campaign(state, ids))
    for row in await state.get_outbox(status="pending_review"):
        await state.update_outbox_item(row["id"], status="approved")


@pytest.mark.asyncio
async def test_each_email_goes_out_through_its_own_mailbox(state):
    config = make_config()
    ids = await seed(state, 3)
    sender = make_sender(state, config)
    await stage_and_approve(state, config, sender, ids)
    await sender._drain_due()
    for address, provider in sender.providers.items():
        assert len(provider.sent) == 1
        assert provider.sent[0]["body"].split("\n\n")[1].startswith(mailbox_for(config, address).name)
    assert await state.count_outbox_sent_today(from_email=BEN) == 1


@pytest.mark.asyncio
async def test_the_daily_cap_is_per_mailbox(state):
    config = make_config(max_daily=1)
    ids = await seed(state, 6)
    sender = make_sender(state, config)
    await stage_and_approve(state, config, sender, ids)
    await sender._drain_due()
    assert [len(p.sent) for p in sender.providers.values()] == [1, 1, 1]
    await sender._drain_due()
    assert [len(p.sent) for p in sender.providers.values()] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_mailbox_still_warming_waits_while_the_others_send(state):
    warming = MailboxConfig(email=BEN, name="Ben", warmup_start=date.today().isoformat())
    config = make_config(mailboxes=[MailboxConfig(email=ANA, name="Ana"), warming])
    ids = await seed(state, 2)
    # One was given Ben before his warmup mattered: it keeps him and waits.
    await state.assign_sender(ids[1], [BEN])
    sender = make_sender(state, config)
    await stage_and_approve(state, config, sender, ids)
    await sender._drain_due()
    assert len(sender.providers[ANA].sent) == 1
    assert sender.providers[BEN].sent == []
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_a_signed_out_mailbox_holds_only_its_own_email(state):
    config = make_config()
    ids = await seed(state, 3)
    providers = {ANA: FakeProvider(), BEN: FakeProvider(configured=False), CAM: FakeProvider()}
    sender = make_sender(state, config, providers)
    await stage_and_approve(state, config, sender, ids)
    await sender._drain_due()
    assert len(providers[ANA].sent) == 1 and len(providers[CAM].sent) == 1
    assert providers[BEN].sent == []
    assert [r["from_email"] for r in await state.get_outbox(status="approved")] == [BEN]


@pytest.mark.asyncio
async def test_an_unread_mailbox_holds_only_its_own_email(state):
    config = make_config()
    ids = await seed(state, 3)
    sender = make_sender(state, config)
    await stage_and_approve(state, config, sender, ids)
    sender.held_mailboxes = {CAM}
    await sender._drain_due()
    assert sender.providers[CAM].sent == []
    assert len(sender.providers[ANA].sent) == 1


@pytest.mark.asyncio
async def test_a_row_with_no_mailbox_goes_from_the_primary(state):
    config = make_config()
    pid = (await seed(state))[0]
    await state.add_outbox_item(
        prospect_id=pid, to_email="p0@acme.example.com", subject="hello there",
        body="Short and fine. Question?", send_at="2020-01-01T00:00:00", status="approved")
    sender = make_sender(state, config)
    await sender._drain_due()
    assert len(sender.providers[ANA].sent) == 1


@pytest.mark.asyncio
async def test_a_row_from_a_removed_mailbox_waits(state):
    config = make_config()
    pid = (await seed(state))[0]
    await state.add_outbox_item(
        prospect_id=pid, to_email="p0@acme.example.com", subject="hello there",
        body="Short and fine. Question?", send_at="2020-01-01T00:00:00", status="approved",
        from_email="gone@sender.example.com")
    sender = make_sender(state, config)
    await sender._drain_due()
    assert all(p.sent == [] for p in sender.providers.values())
    assert len(await state.get_outbox(status="approved")) == 1


# ── replies ──


def inbound(from_email, provider_id="m1"):
    return InboundMessage(provider_id=provider_id, from_email=from_email, subject="hello",
                          body="Tell me more?", message_id="<in-1@x>", thread_ref="t9")


@pytest.mark.asyncio
async def test_a_reply_is_answered_from_the_mailbox_it_arrived_in(state):
    config = make_config()
    pid = (await seed(state, status="contacted"))[0]
    await state.assign_sender(pid, [BEN])
    providers = {ANA: FakeProvider(), BEN: FakeProvider(), CAM: FakeProvider()}
    providers[BEN].inbound = [inbound("p0@acme.example.com")]
    handler = make_handler(state, config, providers)
    assert await handler._run_native() is True
    rows = await state.get_outbox(status="pending_review")
    assert [r["from_email"] for r in rows] == [BEN]
    assert rows[0]["thread_ref"] == "t9"
    assert all(p.confirmed == 1 for p in providers.values())


@pytest.mark.asyncio
async def test_the_reply_is_written_as_that_person(state):
    config = make_config()
    pid = (await seed(state, status="contacted"))[0]
    await state.assign_sender(pid, [BEN])
    providers = {ANA: FakeProvider(), BEN: FakeProvider(), CAM: FakeProvider()}
    providers[BEN].inbound = [inbound("p0@acme.example.com")]
    handler = make_handler(state, config, providers)
    await handler._run_native()
    assert "Write as Ben." in handler.brain.last_prompt


@pytest.mark.asyncio
async def test_mail_between_our_own_mailboxes_is_ignored(state):
    config = make_config()
    providers = {ANA: FakeProvider(), BEN: FakeProvider(), CAM: FakeProvider()}
    providers[ANA].inbound = [inbound(BEN)]
    handler = make_handler(state, config, providers)
    assert await handler._run_native() is True
    assert await state.get_outbox(status="pending_review") == []
    assert await state.is_reply_processed("m1")


@pytest.mark.asyncio
async def test_one_mailbox_that_cant_be_read_holds_only_itself(state):
    config = make_config()
    providers = {ANA: FakeProvider(), BEN: FakeProvider(read_error="boom"), CAM: FakeProvider()}
    handler = make_handler(state, config, providers)
    assert await handler._run_native() is True
    assert handler.unread_mailboxes == {BEN}
    assert BEN in handler.last_read_problem
    assert providers[BEN].confirmed == 0 and providers[ANA].confirmed == 1


@pytest.mark.asyncio
async def test_when_no_mailbox_can_be_read_everything_waits(state):
    config = make_config()
    providers = {b.email: FakeProvider(read_error="boom") for b in mailboxes_of(config)}
    handler = make_handler(state, config, providers)
    assert await handler._run_native() is False


@pytest.mark.asyncio
async def test_a_signed_out_mailbox_is_not_a_read_failure(state):
    config = make_config()
    providers = {ANA: FakeProvider(), BEN: FakeProvider(configured=False), CAM: FakeProvider()}
    handler = make_handler(state, config, providers)
    assert await handler._run_native() is True
    assert handler.unread_mailboxes == set()


@pytest.mark.asyncio
async def test_the_same_gmail_id_in_two_mailboxes_is_two_messages(state):
    config = make_config()
    await seed(state, 1, status="contacted")
    providers = {ANA: FakeProvider(), BEN: FakeProvider(), CAM: FakeProvider()}
    providers[ANA].inbound = [inbound("nobody@elsewhere.example.com", "same")]
    providers[BEN].inbound = [inbound("nobody@elsewhere.example.com", "same")]
    handler = make_handler(state, config, providers)
    await handler._run_native()
    assert await state.is_reply_processed("same")                # primary keeps the bare id
    assert await state.is_reply_processed(f"{BEN}:same")


# ── sign-in ──


def test_the_persona_keeps_the_original_token_file(monkeypatch, tmp_path):
    from closer.integrations import gmail
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path)
    config = make_config()
    assert token_path_for(config, ANA) is None
    assert token_path_for(config, "") is None
    assert token_path_for(config, BEN) == tmp_path / f"{BEN}.json"
    with pytest.raises(ValueError):
        token_path_for(config, "../evil@sender.example.com")


def test_each_mailbox_reads_its_own_sign_in(monkeypatch, tmp_path):
    from closer.integrations import gmail
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path)
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "primary.json")
    config = make_config()
    ben = GmailProvider(config, Env(), mailbox=mailbox_for(config, BEN))
    assert not ben.is_configured()
    gmail._save_token({"refresh_token": "r-ben"}, tmp_path / f"{BEN}.json")
    assert ben.is_configured()
    assert _load_token(tmp_path / f"{BEN}.json")["refresh_token"] == "r-ben"
    assert not GmailProvider(config, Env(), mailbox=mailbox_for(config, ANA)).is_configured()
    assert (tmp_path / f"{BEN}.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_email_is_from_the_mailboxs_person(monkeypatch):
    config = make_config()
    provider = GmailProvider(config, Env(), mailbox=mailbox_for(config, BEN))
    captured = {}

    async def fake_request(method, path, sending=False, json=None, params=None):
        if method == "POST":
            captured.update(json)
            return {"id": "g1", "threadId": "t1"}
        return {}
    monkeypatch.setattr(provider, "_request", fake_request)
    monkeypatch.setattr("closer.integrations.gmail.load_signature", lambda c: None)
    assert (await provider.send_email("to@acme.example.com", "hi", "Body")).ok
    import base64
    raw = base64.urlsafe_b64decode(captured["raw"]).decode()
    assert f"From: Ben <{BEN}>" in raw


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def test_a_sign_in_as_the_wrong_account_is_refused(monkeypatch, capsys):
    import closer.integrations.gmail as gmail
    monkeypatch.setattr(gmail.httpx, "get", lambda *a, **k: _Resp(200, {"emailAddress": ANA}))
    assert not _signed_in_as("token", BEN)
    assert "Nothing saved" in capsys.readouterr().out
    monkeypatch.setattr(gmail.httpx, "get", lambda *a, **k: _Resp(200, {"emailAddress": BEN.upper()}))
    assert _signed_in_as("token", BEN)
    monkeypatch.setattr(gmail.httpx, "get", lambda *a, **k: _Resp(500, {}))
    assert not _signed_in_as("token", BEN)


# ── the pool ──


def test_the_pool_has_one_provider_per_mailbox():
    pool = get_mail_providers(make_config(), Env())
    assert list(pool) == [ANA, BEN, CAM]
    assert pool[BEN].mailbox.name == "Ben"


def test_the_pool_is_gmail_only():
    from closer.config import ConfigError
    config = make_config()
    config.channels.email.provider = "smtp"
    with pytest.raises(ConfigError):
        get_mail_providers(config, Env())


@pytest.mark.asyncio
async def test_sender_of_is_the_persona_with_one_mailbox(state):
    config = make_config(mailboxes=[])
    pid = (await seed(state))[0]
    assert (await sender_of(state, config, pid)).email == ANA
    assert await state.get_prospect_sender(pid) == ""


@pytest.mark.asyncio
async def test_counting_a_mailboxs_sends_includes_old_rows_for_the_primary(state):
    pid = (await seed(state))[0]
    for address in ("", ANA, BEN):
        item = await state.add_outbox_item(
            prospect_id=pid, to_email="p0@acme.example.com", subject="s", body="b",
            send_at="2020-01-01T00:00:00", status="approved", from_email=address,
            kind="reply", conversation_id=address or "none")
        from datetime import datetime, timezone
        await state.update_outbox_item(
            item, status="sent",
            sent_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    assert await state.count_outbox_sent_today() == 3
    assert await state.count_outbox_sent_today(from_email=ANA, primary=True) == 2
    assert await state.count_outbox_sent_today(from_email=ANA) == 1
    assert await state.count_outbox_sent_today(from_email=BEN) == 1


@pytest.mark.asyncio
async def test_new_prospects_skip_a_mailbox_still_in_warmup(state):
    from datetime import date as _d, timedelta
    fresh = (_d.today() - timedelta(days=2)).isoformat()
    ready = (_d.today() - timedelta(days=60)).isoformat()
    config = make_config(
        mailboxes=[MailboxConfig(email=ANA, name="Ana", warmup_start=fresh),
                   MailboxConfig(email=BEN, name="Ben", warmup_start=ready)],
        warmup={"start_date": ready})
    ids = await seed(state, 3)
    assert [(await sender_of(state, config, i)).email for i in ids] == [BEN, BEN, BEN]


@pytest.mark.asyncio
async def test_with_no_mailbox_ready_all_stay_choices(state):
    from datetime import date as _d, timedelta
    fresh = (_d.today() - timedelta(days=2)).isoformat()
    config = make_config(
        mailboxes=[MailboxConfig(email=ANA, name="Ana"), MailboxConfig(email=BEN, name="Ben")],
        warmup={"start_date": fresh})
    ids = await seed(state, 2)
    assert [(await sender_of(state, config, i)).email for i in ids] == [ANA, BEN]
