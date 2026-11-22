"""No draft is written for a catch-all address that send_to_risky won't let go out."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from closer.state import StateManager, WRITABLE_EMAIL_STATUSES, writable_email_statuses


def _config(send_to_risky):
    return SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(send_to_risky=send_to_risky)))


def test_statuses_follow_send_to_risky():
    assert writable_email_statuses(_config(False)) == ("verified",)
    assert writable_email_statuses(_config(True)) == ("verified", "risky")
    assert writable_email_statuses(SimpleNamespace()) == WRITABLE_EMAIL_STATUSES


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


@pytest.mark.asyncio
async def test_risky_prospects_are_not_counted_as_writable_when_risky_is_off(state):
    from closer.models.prospect import Prospect
    for n, status in enumerate(["verified", "risky", "risky"]):
        await state.add_prospect(Prospect(first_name=f"P{n}", last_name="Example",
                                          email=f"p{n}@example.com", email_status=status,
                                          company="Example Co"))
    assert await state.count_writable_prospects() == 3
    state.writable_statuses = writable_email_statuses(_config(False))
    assert await state.count_writable_prospects() == 1
