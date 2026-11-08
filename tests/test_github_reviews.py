"""Eve's automated PR reviews from GitHub webhooks (github_app.py, github_hooks.py, github_review.py)."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentcore.config import GitHubConfig, GitHubRepo
from agentcore.github_app import GitHubApp, GitHubError, commentable_lines
from agentcore.github_hooks import GitHubReviews, verify_signature
from agentcore import github_review as gr

from .conftest import make_agent


@pytest.fixture(autouse=True)
def _as_eve():
    from agentcore.org import set_agent
    set_agent("Eve (AI)", "A. Owner")  # PR reviews are Eve's

SECRET = "whsec"
REPO = "example/app"
PATCH = "@@ -10,3 +10,4 @@ def f():\n     a = 1\n-    b = 2\n+    b = 3\n+    c = b / 0\n     return a\n"


def rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


PEM = rsa_pem()


def pr_json(number=7, head="h1", draft=False, labels=(), login="alex", body="Adds a thing for OE-492",
            fork=False, state="open"):
    base_repo = {"full_name": REPO}
    return {"number": number, "title": "Add division", "body": body, "state": state, "draft": draft,
            "user": {"login": login}, "labels": [{"name": l} for l in labels],
            "head": {"sha": head, "ref": "feature/x", "repo": {"full_name": "someone/fork" if fork else REPO}},
            "base": {"sha": "b0", "ref": "main", "repo": base_repo}}


class FakeGitHub:
    def __init__(self):
        self.pr = pr_json()
        self.commits = [{"commit": {"message": "feat: add c\n\nCo-Authored-By: Claude <noreply@anthropic.com>"}}]
        self.files = [{"filename": "app.py", "status": "modified", "additions": 2, "deletions": 1, "patch": PATCH},
                      {"filename": "package-lock.json", "status": "modified", "additions": 900, "deletions": 3, "patch": "x"},
                      {"filename": "logo.png", "status": "added", "additions": 0, "deletions": 0}]
        self.compare = {"status": "ahead", "files": []}
        self.posted, self.comments, self.replies, self.open = [], [], [], [self.pr]

    def __call__(self, req: httpx.Request):
        p, m = req.url.path, req.method
        if p.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        if p.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "itok"})
        if m == "GET" and p.endswith("/pulls/7"):
            return httpx.Response(200, json=self.pr)
        if p.endswith("/pulls/7/commits"):
            return httpx.Response(200, json=self.commits)
        if p.endswith("/pulls/7/files"):
            return httpx.Response(200, json=self.files)
        if "/compare/" in p:
            return httpx.Response(200, json=self.compare)
        if "/contents/" in p:
            if p.endswith("CLAUDE.md"):
                return httpx.Response(200, json={"encoding": "base64", "size": 20, "content": "IyBSdWxlczogdGVzdCBldmVyeXRoaW5n"})
            return httpx.Response(404, json={})
        if m == "POST" and p.endswith("/pulls/7/reviews"):
            self.posted.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 99})
        if m == "POST" and p.endswith("/issues/7/comments"):
            self.comments.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 5})
        if m == "POST" and "/replies" in p:
            self.replies.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 6})
        if p.endswith("/pulls") or "pulls" in p and req.url.params.get("state") == "open":
            return httpx.Response(200, json=self.open)
        raise AssertionError(f"unexpected {m} {p}")


def make(cfg_agent, script, fake=None, now=None):
    fake = fake or FakeGitHub()
    agent, model, store, tools = make_agent(cfg_agent, script)
    gh = GitHubApp("123", PEM, http=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    clock = [now or datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)]
    hooks = GitHubReviews(agent=agent, store=store, gh=gh, cfg=GitHubConfig(enabled=True, command="/eve", skip_label="no-eve", repos={REPO: GitHubRepo()}),
                          clock=lambda: clock[0])
    return hooks, fake, model, store, clock


def signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


REVIEW_JSON = json.dumps({"summary": "Adds c; divides by zero.", "findings": [
    {"pass": 3, "severity": "Major", "title": "Division by zero", "detail": "b / 0 always raises; guard it.",
     "path": "app.py", "line": 12, "side": "RIGHT"},
    {"pass": 1, "severity": "Minor", "title": "No test", "detail": "CLAUDE.md asks for tests.", "path": None, "line": None},
    {"pass": 3, "severity": "Note", "title": "Off the diff", "detail": "line 40 is outside the change",
     "path": "app.py", "line": 40}]})


def test_signatures_and_line_mapping():
    body, sig = signed({"a": 1})
    assert verify_signature(SECRET, body, sig) and not verify_signature(SECRET, body + b" ", sig)
    assert not verify_signature(SECRET, body, None) and not verify_signature("", body, sig)
    lines = commentable_lines(PATCH)
    assert lines["RIGHT"] == {10, 11, 12, 13} and lines["LEFT"] == {10, 11, 12}


async def test_webhooks_are_verified_deduplicated_and_filtered(cfg):
    hooks, fake, model, store, clock = make(cfg, [])
    body, sig = signed({"action": "opened", "repository": {"full_name": REPO}, "sender": {"login": "alex", "type": "User"},
                        "pull_request": pr_json()})
    assert (await hooks.receive("pull_request", "d1", body, "sha256=bad", SECRET))[0] == 401
    assert await hooks.receive("pull_request", "d1", body, sig, SECRET) == (202, "queued")
    assert await hooks.receive("pull_request", "d1", body, sig, SECRET) == (202, "duplicate")
    job = await store.get_pref(f"{REPO}#7", "github-job")
    assert job["head"] == "h1" and job["full"]
    for change, why in ((dict(draft=True), "draft"), (dict(labels=("no-eve",)), "no-eve"), (dict(fork=True), "fork"),
                        (dict(login="dependabot[bot]"), "bot author")):
        b, s = signed({"action": "opened", "repository": {"full_name": REPO}, "sender": {"login": "x", "type": "User"},
                       "pull_request": pr_json(**change)})
        assert (await hooks.receive("pull_request", f"d-{why}", b, s, SECRET))[1] == f"skipped: {why}"
    b, s = signed({"action": "opened", "repository": {"full_name": "example/other"}, "sender": {"type": "User"}})
    assert (await hooks.receive("pull_request", "d9", b, s, SECRET))[1] == "repo not opted in"
    b, s = signed({"action": "created", "repository": {"full_name": REPO}, "sender": {"login": "eve-ai[bot]", "type": "Bot"}})
    assert (await hooks.receive("issue_comment", "d10", b, s, SECRET))[1] == "bot"


async def test_a_burst_of_pushes_becomes_one_review_after_the_quiet_period(cfg):
    hooks, fake, model, store, clock = make(cfg, [REVIEW_JSON])
    for i, head in enumerate(("h1", "h2", "h3")):
        b, s = signed({"action": "synchronize", "repository": {"full_name": REPO}, "sender": {"type": "User", "login": "a"},
                       "pull_request": pr_json(head=head)})
        await hooks.receive("pull_request", f"p{i}", b, s, SECRET)
    assert await hooks.tick() == 0  # still inside the 90 s quiet period
    fake.pr = pr_json(head="h3")
    clock[0] += timedelta(seconds=91)
    assert await hooks.tick() == 1 and len(fake.posted) == 1 and fake.posted[0]["commit_id"] == "h3"


async def test_the_review_is_posted_with_inline_comments_only_where_github_accepts_them(cfg):
    hooks, fake, model, store, clock = make(cfg, [REVIEW_JSON])
    await hooks.enqueue(REPO, 7, "h1", force_full=True, due_now=True)
    assert await hooks.tick() == 1
    [review] = fake.posted
    assert review["event"] == "COMMENT" and review["commit_id"] == "h1"
    assert [(c["path"], c["line"]) for c in review["comments"]] == [("app.py", 12)]
    assert "Division by zero" in review["comments"][0]["body"]
    body = review["body"]
    assert "No test" in body and "Off the diff" in body and "line 40" in body  # moved to the summary, not dropped
    assert "package-lock.json (excluded)" in body and "logo.png (binary" in body
    assert "Co-Authored-By: Claude" in body and "doesn't approve" in body and "/eve" in body
    prompt = str(model.calls[0]["messages"])
    assert "c = b / 0" in prompt and "Rules: test everything" in prompt and "OE-492" in prompt
    assert model.calls[0]["tier"] != "fast"  # no first responder on a PR
    done = await store.get_pref(f"{REPO}#7", "github-reviewed")
    assert done["head"] == "h1" and done["review_id"] == 99
    # a resumed job for the same head never posts twice
    await hooks.enqueue(REPO, 7, "h1", due_now=True)
    await hooks.tick()
    assert len(fake.posted) == 1


async def test_a_push_on_top_is_reviewed_incrementally(cfg):
    hooks, fake, model, store, clock = make(cfg, [REVIEW_JSON, json.dumps({"summary": "Guard added.", "findings": [],
                                                                            "addressed": ["Division by zero"]})])
    await hooks.enqueue(REPO, 7, "h1", force_full=True, due_now=True)
    await hooks.tick()
    fake.pr = pr_json(head="h2")
    fake.compare = {"status": "ahead", "files": [{"filename": "app.py", "status": "modified", "additions": 1,
                                                  "deletions": 1, "patch": "@@ -12,1 +12,1 @@\n-    c = b / 0\n+    c = b / 1\n"}]}
    await hooks.enqueue(REPO, 7, "h2", due_now=True)
    await hooks.tick()
    second = fake.posted[1]
    assert "new changes since my last review" in second["body"] and "Now looks addressed" in second["body"]
    assert "RE-REVIEW" in str(model.calls[-1]["messages"]) and "Division by zero" in str(model.calls[-1]["messages"])


def test_secrets_are_redacted_and_author_evidence_read():
    text, n = gr.redact('key = "AKIAABCDEFGHIJKLMNOP"\npassword: "hunter2hunter2xx"\nurl = postgres://u:pw123@db/x\n'
                        'token = os.environ["T"]')
    assert n == 3 and "AKIA" not in text and "hunter2" not in text and "pw123" not in text and 'os.environ["T"]' in text
    said, ev = gr.author_evidence(pr_json(), [{"commit": {"message": "x\n\nCo-Authored-By: Claude <c>"}}])
    assert said == "Claude"
    said, ev = gr.author_evidence(pr_json(login="chatgpt-codex-connector[bot]"), [{"commit": {"message": "Co-Authored-By: Claude"}}])
    assert said == "not sure"  # two makers: avoid both
    assert gr.author_evidence(pr_json(), [{"commit": {"message": "plain"}}])[0] == "not sure"
    # Acme's default (14 Oct 2026): an unmarked PR, or Copilot/Cursor, was drafted with Claude
    said, ev = gr.author_evidence(pr_json(), [{"commit": {"message": "plain"}}], assume="Claude")
    assert said == "Claude" and "assumed Claude" in ev[0]
    assert gr.author_evidence(pr_json(), [{"commit": {"message": "Co-Authored-By: Copilot"}}], assume="Claude")[0] == "Claude"
    assert gr.author_evidence(pr_json(login="chatgpt-codex-connector[bot]"), [], assume="Claude")[0] == "ChatGPT"


async def test_approve_is_refused_whatever_asks():
    gh = GitHubApp("1", PEM, http=httpx.AsyncClient(transport=httpx.MockTransport(FakeGitHub())))
    with pytest.raises(GitHubError, match="never approves"):
        await gh.create_review(REPO, 7, "h1", "lgtm", [], event="APPROVE")


async def test_the_review_turn_has_read_tools_only(cfg):
    from agentcore.tools import CallContext
    hooks, fake, model, store, clock = make(cfg, [])
    gw = hooks.agent.tools
    ctx = CallContext("github:example/app#7", "github", [], allowed_tools=set(gr.REVIEW_TOOLS))
    out = await gw.call("mail__send_email", {"to": ["x@acme.example"]}, ctx, True)
    assert out.status == "blocked" and "not available in this kind of conversation" in out.content


async def test_follow_ups_reply_in_the_thread(cfg):
    hooks, fake, model, store, clock = make(cfg, ["Because b / 0 raises ZeroDivisionError."])
    b, s = signed({"action": "created", "repository": {"full_name": REPO}, "sender": {"login": "alex", "type": "User"},
                   "pull_request": {"number": 7}, "comment": {"id": 41, "body": "/eve why is this Major?",
                                                              "user": {"login": "alex"}, "path": "app.py", "line": 12}})
    assert (await hooks.receive("pull_request_review_comment", "f1", b, s, SECRET))[1] == "queued follow-up"
    await hooks.tick()
    assert fake.replies and "ZeroDivisionError" in fake.replies[0]["body"]
    b, s = signed({"action": "created", "repository": {"full_name": REPO}, "sender": {"login": "alex", "type": "User"},
                   "issue": {"number": 7, "pull_request": {}}, "comment": {"id": 42, "body": "thanks all"}})
    assert (await hooks.receive("issue_comment", "f2", b, s, SECRET))[1] == "not for this reviewer"


async def test_caps_hold_reviews_until_tomorrow(cfg):
    hooks, fake, model, store, clock = make(cfg, [REVIEW_JSON])
    hooks.cfg.limits.reviews_per_day = 0
    await hooks.enqueue(REPO, 7, "h1", due_now=True)
    await hooks.tick()
    assert not fake.posted
    job = await store.get_pref(f"{REPO}#7", "github-job")
    assert datetime.fromisoformat(job["due"]) > clock[0] + timedelta(hours=10)


async def test_reconcile_queues_open_prs_without_a_review(cfg):
    hooks, fake, model, store, clock = make(cfg, [])
    assert await hooks.reconcile() == 1
    assert (await store.get_pref(f"{REPO}#7", "github-job"))["head"] == "h1"
    await store.set_pref(f"{REPO}#7", "github-job", None)
    await store.set_pref(f"{REPO}#7", "github-reviewed", {"head": "h1"})
    assert await hooks.reconcile() == 0


async def test_tests_come_before_generated_data_and_a_repo_without_rules_is_said_plainly(monkeypatch):
    """pipeline-dashboard#46 (14 Oct 2026): a generated JSON file used the diff budget and every test was skipped,
    and missing CLAUDE.md/AGENTS.md read as checks that couldn't be done."""
    fake = FakeGitHub()
    big = "+" + "x" * 900 + "\n"
    fake.files = [{"filename": "src/generated/meta.json", "status": "added", "additions": 300, "deletions": 0, "patch": big * 300},
                  {"filename": "README.md", "status": "modified", "additions": 5, "deletions": 0, "patch": big * 5},
                  {"filename": "src/server.ts", "status": "added", "additions": 200, "deletions": 0, "patch": big * 200},
                  {"filename": "test/server.test.ts", "status": "added", "additions": 100, "deletions": 0, "patch": big * 100}]
    gh = GitHubApp("1", PEM, http=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(gr, "MAX_PATCH_CHARS", 300 * 1000)
    g = await gr.gather(gh, REPO, 7, ["*.lock"])
    names = [f["filename"] for f in g["files"]]
    assert names[:2] == ["src/server.ts", "test/server.test.ts"]  # source, then tests
    assert "src/generated/meta.json (excluded)" in g["skipped"]
    assert "Rules: test everything" in g["rules"]  # this repo has a CLAUDE.md
    fake_no_rules = FakeGitHub()
    fake_no_rules.__class__ = type("NoRules", (FakeGitHub,), {"__call__": lambda self, req: httpx.Response(404, json={})
                                   if "/contents/" in req.url.path else FakeGitHub.__call__(self, req)})
    gh2 = GitHubApp("1", PEM, http=httpx.AsyncClient(transport=httpx.MockTransport(fake_no_rules)))
    g2 = await gr.gather(gh2, REPO, 7, [])
    assert "has none of CLAUDE.md" in g2["rules"] and "Conventional Commits" in g2["rules"]
    assert gr.review_order("mcp/test/model.test.ts") < gr.review_order("mcp/README.md") < gr.review_order("data.json")
