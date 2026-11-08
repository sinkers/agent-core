import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agentcore.killswitch import KillSwitch
from agentcore.skills import SkillLoader
from agentcore.teams import AuthError, BotAuthenticator, conversation_reference, is_addressed, message_text

from .conftest import SKILLS


class _Key:
    def __init__(self, key):
        self.key = key


class StaticJwks:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return _Key(self.public_key)


@pytest.fixture(scope="module")
def keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k, k.public_key()


def token(priv, **over):
    claims = {"iss": "https://api.botframework.com", "aud": "app-123", "exp": int(time.time()) + 600,
              "serviceurl": "https://smba.trafficmanager.net/au/"}
    claims.update(over)
    return jwt.encode(claims, priv, algorithm="RS256")


async def test_valid_bot_token_accepted(keypair):
    priv, pub = keypair
    auth = BotAuthenticator("app-123", jwks_client=StaticJwks(pub))
    claims = await auth.verify(f"Bearer {token(priv)}", "https://smba.trafficmanager.net/au")
    assert claims["aud"] == "app-123"


@pytest.mark.parametrize("over,header,service", [
    ({"aud": "someone-else"}, None, None),
    ({"iss": "https://evil"}, None, None),
    ({"exp": int(time.time()) - 3600}, None, None),
    ({}, "", None),
    ({}, None, "https://other.example/"),
])
async def test_bad_bot_tokens_rejected(keypair, over, header, service):
    priv, pub = keypair
    auth = BotAuthenticator("app-123", jwks_client=StaticJwks(pub))
    hdr = header if header is not None else f"Bearer {token(priv, **over)}"
    with pytest.raises(AuthError):
        await auth.verify(hdr, service or "https://smba.trafficmanager.net/au/")


def activity(conv_type="personal", text="hello", mention=False):
    a = {"type": "message", "id": "m1", "text": text, "serviceUrl": "https://smba/",
         "channelId": "msteams", "conversation": {"id": "conv1", "conversationType": conv_type},
         "from": {"id": "29:u", "aadObjectId": "aad-u", "name": "Alex"},
         "recipient": {"id": "28:bot", "name": "Ada (AI)"},
         "channelData": {"tenant": {"id": "t1"}}}
    if mention:
        a["text"] = "<at>Ada (AI)</at> " + text
        a["entities"] = [{"type": "mention", "text": "<at>Ada (AI)</at>", "mentioned": {"id": "28:bot"}}]
    return a


def test_addressing_rules():
    assert is_addressed(activity("personal"), "28:bot", "Ada")
    assert not is_addressed(activity("groupChat", "lunch?"), "28:bot", "Ada")
    assert is_addressed(activity("channel", "status", mention=True), "28:bot", "Ada")
    assert is_addressed(activity("groupChat", "Ada, where is AU05 up to?"), "28:bot", "Ada")
    assert not is_addressed(activity("groupChat", "Adaon said hi"), "28:bot", "Ada")


def test_mention_stripped_and_reference_kept():
    a = activity("channel", "status please", mention=True)
    assert message_text(a, "28:bot") == "status please"
    ref = conversation_reference(a)
    assert ref["tenantId"] == "t1" and ref["bot"]["id"] == "28:bot" and ref["conversation"]["id"] == "conv1"


@pytest.mark.parametrize("agent", ["ada", "eve", "dan"])
def test_skill_loader_for_each_agent(agent):
    from agentcore.config import load_agent_config

    from .conftest import AGENTS
    cfg = load_agent_config(AGENTS / agent / "agent.yaml")
    loader = SkillLoader(SKILLS, agent, cfg.skills.always, cfg.skills.on_demand)
    block = loader.system_prompt_block()
    for name in cfg.skills.always:
        assert f'<skill name="{name}"' in block
    for name in cfg.skills.on_demand:
        assert f"- {name}:" in block and f'<skill name="{name}"' not in block
        assert loader.load(name).startswith(f'<skill name="{name}"')


def test_skill_not_applying_to_agent_is_refused():
    with pytest.raises(ValueError):
        SkillLoader(SKILLS, "dan", ["today-board"], [])  # today-board applies to ada only


def test_killswitch_values_and_fail_safe(monkeypatch):
    monkeypatch.delenv("KILLSWITCH_OVERRIDE", raising=False)
    ks = KillSwitch("eve")
    assert not ks.outbound_allowed  # stopped until first read
    ks.apply({"eve": "run", "ada": "stop"})
    assert ks.outbound_allowed
    ks.apply({"eve": "stop"})
    assert not ks.outbound_allowed
    ks.apply({})
    assert ks.outbound_allowed  # key absent means run
