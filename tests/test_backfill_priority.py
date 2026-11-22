"""Backfill checks companies from a user-supplied list before stumbled-on ones."""

import os
import tempfile

import pytest
import pytest_asyncio

from closer.models.company import Company
from closer.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


@pytest.mark.asyncio
async def test_listed_companies_come_first(state):
    for name, source in [("Older Co", "web_search"), ("Newer Co", "web_search"),
                         ("Listed Co", "csv:sample.csv")]:
        domain = name.split()[0].lower() + ".example.com"
        await state.add_company(Company(name=name, domain=domain, source=source))
    got = await state.companies_needing_prospects(limit=3)
    assert got[0]["domain"] == "listed.example.com"
    assert len(got) == 3


@pytest.mark.asyncio
async def test_backfill_reads_the_configured_number_of_companies(state):
    from types import SimpleNamespace
    from closer.agents.scout import Scout
    for n in range(5):
        await state.add_company(Company(name=f"Co{n}", domain=f"co{n}.example.com",
                                        source="csv:sample.csv"))
    config = SimpleNamespace(icp=SimpleNamespace(industries=[], known_companies_per_cycle=3))
    scout = Scout(None, state, config, None)
    seen = []

    async def fake_contacts(**kw):
        seen.append(kw["domain"])
        return []
    scout._contacts_from_company = fake_contacts
    await scout._prospect_via_known_companies()
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_backfill_stops_starting_companies_after_its_time_budget(state, monkeypatch):
    from types import SimpleNamespace
    from closer.agents import scout as scout_mod
    for n in range(4):
        await state.add_company(Company(name=f"Co{n}", domain=f"co{n}.example.com",
                                        source="csv:sample.csv"))
    clock = iter([0, 0, 10, 1000, 1000, 1000])
    monkeypatch.setattr(scout_mod, "_clock", lambda: next(clock))
    monkeypatch.setattr(scout_mod, "BACKFILL_TIME_BUDGET_SECONDS", 100)
    config = SimpleNamespace(icp=SimpleNamespace(industries=[], known_companies_per_cycle=4))
    scout = scout_mod.Scout(None, state, config, None)
    seen = []

    async def fake_contacts(**kw):
        seen.append(kw["domain"])
        return []
    scout._contacts_from_company = fake_contacts
    await scout._prospect_via_known_companies()
    assert len(seen) == 2
    # The two it didn't reach are still waiting for the next cycle.
    assert len(await state.companies_needing_prospects(limit=10)) == 2


@pytest.mark.asyncio
async def test_lists_only_mode_never_backfills_stumbled_on_companies(state):
    from types import SimpleNamespace
    from closer.agents.scout import Scout
    await state.add_company(Company(name="Found Co", domain="found.example.com", source="web_search"))
    await state.add_company(Company(name="Listed Co", domain="listed.example.com", source="csv:sample.csv"))
    config = SimpleNamespace(icp=SimpleNamespace(
        industries=[], known_companies_per_cycle=5, known_companies_only=True))
    scout = Scout(None, state, config, None)
    seen = []

    async def fake_contacts(**kw):
        seen.append(kw["domain"])
        return []
    scout._contacts_from_company = fake_contacts
    await scout._prospect_via_known_companies()
    assert seen == ["listed.example.com"]
    # With the list worked through, nothing else is picked up.
    seen.clear()
    await scout._prospect_via_known_companies()
    assert seen == []
