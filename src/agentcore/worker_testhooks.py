"""Test hooks for the scale-to-zero PoC, active only in a worker started with WORKER_TEST_HOOKS=1.

A chat message that is exactly one of these runs the hook instead of a turn:
  __crash__       the worker dies mid-item (no result, no goodbye): the retry cap and dead letter
  __sleep N__     a task of N seconds with heartbeats: long work outliving the lease
  __attack__      from inside the worker's sandbox, try what a hijacked worker would: reach hosts outside its
                  policy, the database, the Kubernetes API, cloud metadata; use the work API beyond its agent;
                  find a real credential; write outside /sandbox and /tmp. Every one should fail.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from pathlib import Path


async def run(text: str, client=None) -> dict | None:
    t = text.strip()
    if t == "__crash__":
        os._exit(3)
    if t.startswith("__sleep ") and t.endswith("__"):
        seconds = float(t[8:-2])
        await asyncio.sleep(seconds)
        return {"reply": f"slept {seconds:.0f} s"}
    if t == "__attack__":
        results = await attack(client)
        failed = [r for r in results if r["outcome"] != "ok"]
        return {"reply": ("ALL AS EXPECTED" if not failed else f"{len(failed)} FAILED") + "\n" +
                json.dumps(results, indent=1), "attack": results}
    return None


async def attack(client) -> list[dict]:
    out: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:  # ok: what should happen did (blocked, or allowed)
        out.append({"attempt": name, "outcome": "ok" if ok else "FAIL", "detail": detail[:200]})

    # 1. Network: hosts outside the policy, directly and through the proxy the sandbox is given.
    import httpx
    for name, url in (("internet via proxy", "https://example.com/"),
                      ("Bedrock control plane", "https://bedrock.ap-southeast-2.amazonaws.com/foundation-models"),
                      ("AWS STS", "https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"),
                      ("Kubernetes API", "https://kubernetes.default.svc/api"),
                      ("cloud metadata", "http://169.254.169.254/latest/meta-data/"),
                      ("OpenShell gateway", "https://openshell.openshell.svc.cluster.local:8080/"),
                      ("front door admin API", "http://frontdoor.agents.svc.cluster.local:8080/v1/admin/status"),
                      ("front door client API", "http://frontdoor.agents.svc.cluster.local:8080/v1/agents/probe/chat")):
        try:
            async with httpx.AsyncClient(timeout=6, verify=False) as h:
                r = await (h.get(url) if "chat" not in url else h.post(url, json={}))
            record(name, r.status_code in (401, 403, 407) or r.status_code >= 500, f"HTTP {r.status_code}")
        except Exception as exc:
            record(name, True, f"{type(exc).__name__}: {exc}")
    def connect(host: str, port: int) -> None:
        socket.create_connection((host, port), timeout=5).close()
    for name, host, port in (("Postgres direct", "pg-rw.platform.svc.cluster.local", 5432),
                             ("Postgres by service IP range", "10.43.2.37", 5432),
                             ("internet direct (no proxy)", "1.1.1.1", 443)):
        try:
            await asyncio.to_thread(connect, host, port)  # off the event loop: heartbeats keep going
            record(name, False, "connected")
        except Exception as exc:
            record(name, True, f"{type(exc).__name__}: {exc}")
    # 2. The work API beyond this agent: a store call that isn't allowed, and a forged token.
    if client is not None:
        r = await client.http.post("/v1/worker/store/claim_due", json={"args": [], "kwargs": {}})
        record("work API: claim schedules (not allowed)", r.status_code == 403, f"HTTP {r.status_code}")
        r = await client.http.post("/v1/worker/store/recent_messages", json={"args": ["tg:1", 5], "kwargs": {}},
                                   headers={"Authorization": "Bearer forged-token"})
        record("work API: forged worker token", r.status_code == 401, f"HTTP {r.status_code}")
        r = await client.http.post("/v1/worker/store/../../admin/status", json={})
        record("work API: path escape to admin", r.status_code in (401, 403, 404), f"HTTP {r.status_code}")
    # 3. Credentials: anything that looks like a real AWS key or secret in the environment or files we can read.
    key_re = re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}")
    found = [k for k, v in os.environ.items() if key_re.search(v or "")]
    # Never echo the values: even a placeholder sent to an endpoint its provider isn't bound to is refused by the
    # proxy (403 credential_endpoint_mismatch), which is the next check.
    aws = {k: ("placeholder" if v.lower().startswith("openshell") else f"other ({len(v)} chars)")
           for k, v in os.environ.items() if k.startswith("AWS_") and ("KEY" in k or "TOKEN" in k)}
    record("real AWS key in environment", not found and all(v == "placeholder" for v in aws.values()),
           f"pattern matches: {found}; AWS_* are: {aws}")
    placeholder = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    if client is not None and placeholder:
        # Store the placeholder through the work API and read it back: if the proxy put the real secret into the
        # request body, the round trip would hand it to the worker. Only yes/no is reported, never the value.
        try:
            r = await client.http.post("/v1/worker/store/set_pref",
                                       json={"args": ["_attack", "smuggled", placeholder], "kwargs": {}})
            if r.status_code != 200:
                record("smuggle the credential placeholder out and back", True, f"set: HTTP {r.status_code}")
            else:
                back = (await client.http.post("/v1/worker/store/get_pref",
                                               json={"args": ["_attack", "smuggled"], "kwargs": {}})).json()["result"]
                same = back == placeholder
                record("smuggle the credential placeholder out and back", same,
                       "came back unchanged (the proxy did not resolve it)" if same
                       else "CAME BACK DIFFERENT: the proxy resolved it in the body")
        except Exception as exc:
            record("smuggle the credential placeholder out and back", True, f"{type(exc).__name__}")
    def scan() -> tuple[list[str], int]:
        hits, seen = [], 0
        for root in ("/sandbox", "/tmp", "/app/agents", "/app/policies", "/etc", "/root", "/home"):
            try:
                for dirpath, _dirs, files in os.walk(root):
                    for f in files:
                        seen += 1
                        if seen > 20000:
                            return hits, seen
                        p = os.path.join(dirpath, f)
                        try:
                            if os.path.getsize(p) < 1_000_000 and key_re.search(Path(p).read_text(errors="ignore")):
                                hits.append(p)
                        except Exception:
                            pass
            except Exception:
                pass
        return hits, seen
    hits, seen = await asyncio.to_thread(scan)
    record("real AWS key in readable files", not hits, f"{seen} files read; matches: {hits[:5]}")
    # 4. Writes outside /sandbox and /tmp.
    for path in ("/app/pwned", "/etc/pwned", "/usr/local/lib/pwned", "/var/log/pwned", "/home/agent/pwned"):
        try:
            Path(path).write_text("x")
            record(f"write {path}", False, "written")
        except Exception as exc:
            record(f"write {path}", True, f"{type(exc).__name__}")
    for path in ("/sandbox/ok", "/tmp/ok"):
        try:
            Path(path).write_text("x")
            record(f"write {path} (allowed)", True, "written")
        except Exception as exc:
            record(f"write {path} (allowed)", False, f"should be writable: {exc}")
    return out
