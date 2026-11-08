"""Security checks on the front door: what a client token can reach, pairing, pools and token handling."""

import httpx
import pytest

from agentcore.frontdoor.app import _api_id, _check
from agentcore.frontdoor.config import FrontDoorConfig
from agentcore.frontdoor.telegram import PAIRING_MAX_TRIES, TelegramBot


def test_api_ids_cannot_name_another_channels_people():
    assert _api_id("tg:12345") == "api:tg:12345"
    assert _api_id("voxhelm:u1") == "api:voxhelm:u1"
    assert _api_id("api:me") == "api:me"
    assert _api_id(None) is None


def test_non_ascii_token_is_refused_not_an_error(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("CLIENT_API_TOKEN", "secret")
    with pytest.raises(HTTPException) as e:
        _check("Bearer t\u00f6ken", "CLIENT_API_TOKEN")
    assert e.value.status_code == 401


@pytest.mark.parametrize("change", [{"image": "other"}, {"policy": "wider.yaml"}, {"providers": ["someone-elses"]}])
def test_agent_must_match_its_pools_sandbox(change):
    pool = {"image": "i", "policy": "p.yaml", "providers": ["a"]}
    agent = {"image": "i", "policy": "p.yaml", "providers": ["a"], "pool": "p"}
    FrontDoorConfig.model_validate({"pools": {"p": pool}, "agents": {"ada": agent}})
    with pytest.raises(ValueError, match="must match pool"):
        FrontDoorConfig.model_validate({"pools": {"p": {**pool, **change}}, "agents": {"ada": agent}})


def _link(handler):
    link = TelegramBot.__new__(TelegramBot)
    link.file_base = "https://api.telegram.org/file/botSECRET"
    link.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    link._pairing_tries = {}
    return link


async def test_failed_download_does_not_put_the_token_in_the_error():
    link = _link(lambda r: httpx.Response(404))
    async def get_file(*a, **k):
        return {"file_path": "voice/1.ogg"}
    link.call = get_file
    with pytest.raises(RuntimeError) as e:
        await link.download("f1")
    assert "SECRET" not in str(e.value)


def test_pairing_locks_after_repeated_wrong_codes():
    link = _link(lambda r: httpx.Response(200))
    assert not link._pairing_locked(7)
    for _ in range(PAIRING_MAX_TRIES):
        link._pairing_failed(7)
    assert link._pairing_locked(7) and not link._pairing_locked(8)
