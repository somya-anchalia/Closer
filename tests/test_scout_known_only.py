"""icp.known_companies_only: Scout works only the companies already on file."""

import pytest

from closer.agents.scout import Scout
from closer.config import load_config


class _Brain:
    def load_skills_for_agent(self, name):
        return ""


class _State:
    async def log_action(self, **kw):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("known_only,expected", [
    (True, ["known_companies"]),
    (False, ["hunter", "known_companies", "hiring_boards", "company_discovery"]),
])
async def test_strategies_follow_the_flag(monkeypatch, known_only, expected):
    config = load_config()
    config.icp.known_companies_only = known_only
    config.channels.linkedin.enabled = False
    from closer.config import EnvConfig
    scout = Scout(_Brain(), _State(), config, EnvConfig())
    ran = []
    for name in ("hunter", "known_companies", "hiring_boards", "company_discovery"):
        async def fake(n=name):
            ran.append(n)
            return 0
        attr = {"hunter": "_prospect_via_hunter", "known_companies": "_prospect_via_known_companies",
                "hiring_boards": "_prospect_via_hiring_boards",
                "company_discovery": "_prospect_via_company_discovery"}[name]
        monkeypatch.setattr(scout, attr, fake)
    await scout.run()
    assert ran == expected
