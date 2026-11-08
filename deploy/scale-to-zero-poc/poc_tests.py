"""The PoC's success criteria and attack tests (docs/plans/scale-to-zero.md, section 11), run against the PoC cluster.

  python3 poc_tests.py [--host USER@HOST] [test ...]

Tests: cold warm attack crash burst schedule killswitch idle poweroff long (default: all but poweroff and long).
Run from deploy/scale-to-zero-poc with ./kubeconfig and tunnels.sh running. API calls and SQL run inside a front door
pod (its tokens and database credentials never leave the cluster); the kill switch is flipped with kubectl; the
power-off test destroys and restarts a VM on the libvirt host (--host). Results are printed and appended to
poc-results.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = {**os.environ, "KUBECONFIG": str(HERE / "kubeconfig")}
POD_HELPERS = r'''
import json, os, sys, time, httpx, psycopg
def api(method, path, env="ADMIN_API_TOKEN", body=None, timeout=90):
    r = httpx.request(method, "http://127.0.0.1:8080" + path, headers={"Authorization": "Bearer " + os.environ[env]},
                      json=body, timeout=timeout)
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}
def sql(q, *a):
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        cur = c.execute(q, a)
        return [list(r) for r in cur.fetchall()] if cur.description else []
'''


def sh(*cmd: str, check: bool = True, input: str | None = None) -> str:
    r = subprocess.run(cmd, env=ENV, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {r.stderr.strip()[:300]}")
    return r.stdout


def ready_frontdoor_pods() -> list[str]:
    """Front door pods that are Ready, on nodes that are Ready (a powered-off node's pods linger)."""
    nodes = json.loads(sh("kubectl", "get", "nodes", "-o", "json"))["items"]
    up = {n["metadata"]["name"] for n in nodes
          if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])}
    pods = json.loads(sh("kubectl", "-n", "agents", "get", "pods", "-l", "app=frontdoor", "-o", "json"))["items"]
    return [p["metadata"]["name"] for p in pods if p["spec"].get("nodeName") in up
            and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]


def pod(code: str) -> object:
    """Run `code` (which sets `out`) in a front door pod on a healthy node; return `out`."""
    script = POD_HELPERS + code + "\nprint('<<OUT>>' + json.dumps(out, default=str))"
    last = None
    for attempt in range(6):
        for name in ready_frontdoor_pods():
            try:
                text = sh("kubectl", "-n", "agents", "exec", "-i", name, "--", "python3", "-", input=script)
                return json.loads(text.split("<<OUT>>", 1)[1])
            except RuntimeError as exc:
                last = exc
        time.sleep(5)
    raise RuntimeError(f"no front door pod answered: {last}")


def chat(text: str, conv: str = "test-poc") -> dict:
    t0 = time.monotonic()
    r = pod(f"out = api('POST', '/v1/agents/probe/chat', 'CLIENT_API_TOKEN', "
            f"{{'conversation_id': {conv!r}, 'text': {text!r}, 'user_id': 'test-poc', 'user_name': 'PoC tests'}})")
    r["round_trip_s"] = round(time.monotonic() - t0, 2)  # includes kubectl exec; the server-side time is in events
    return r


def result(item_id: int, wait: int = 60) -> dict:
    return pod(f"out = api('GET', '/v1/work/{item_id}?wait={wait}', 'CLIENT_API_TOKEN', timeout={wait + 30})")


def wait_done(item_id: int, timeout: float) -> dict:
    end = time.monotonic() + timeout
    while True:
        r = result(item_id, 50)
        if r["status"] in ("done", "dead") or time.monotonic() > end:
            return r
        time.sleep(1)


def events(item_id: int | None = None, kind: str | None = None, after: int = 0) -> list[dict]:
    rows = pod(f"out = sql(\"SELECT id, at, kind, detail FROM frontdoor.events WHERE id > %s ORDER BY id\", {after})")
    evs = [{"id": r[0], "at": r[1], "kind": r[2], **(r[3] or {})} for r in rows]
    return [e for e in evs if (item_id is None or e.get("item") == item_id) and (kind is None or e["kind"] == kind)]


def last_event_id() -> int:
    return pod("out = sql('SELECT coalesce(max(id), 0) FROM frontdoor.events')[0][0]")


def live_workers() -> list[dict]:
    return pod("out = api('GET', '/v1/admin/status')")["workers"]


def sandbox_pods() -> list[str]:
    return [line for line in sh("kubectl", "-n", "sandboxes", "get", "pods", "--no-headers", check=False).splitlines()
            if line.strip()]


def probe_workers() -> list[dict]:
    """Probe's own workers (pooled ones waiting for an agent are shared, not Probe's)."""
    return [w for w in live_workers() if w["agent"] == "probe"]


def pooled_waiting() -> int:
    return sum(1 for w in live_workers() if w["agent"] == "" and w["state"] == "pooled")


def wait_no_workers(timeout: float) -> float | None:
    """Until Probe has no worker and no sandbox of its own (the pool's waiting sandboxes don't count)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        mine = probe_workers()
        handles = {w["sandbox"] for w in live_workers() if w["agent"] == ""}
        others = [p for p in sandbox_pods() if not any(h and h in p for h in handles) and "supervisor" not in p]
        if not mine and not others:
            return round(time.monotonic() - t0, 1)
        time.sleep(2)
    return None


def set_killswitch(value: str) -> None:
    sh("kubectl", "-n", "agents", "patch", "configmap", "agents-killswitch", "--type", "merge",
       "-p", json.dumps({"data": {"probe": value}}))


def timings(item_id: int) -> dict:
    ev = {e["kind"]: e for e in events(item_id)}
    out = {}
    if "intake" in ev:
        out["holding_line_ms"] = ev["intake"].get("holding_ms")
        out["warm"] = ev["intake"].get("warm")
    if "claimed" in ev:
        out["queued_s"] = round(ev["claimed"]["queued_s"], 2)
    if "done" in ev:
        out["answer_s"] = round(ev["done"]["total_s"], 2)
        out["turn_s"] = ev["done"].get("work_s")
    return out


# --- tests ----------------------------------------------------------------------------------------------------
def t_cold(args) -> dict:
    """Asleep (no worker) to answer: holding line under 1 s, answer under 10 s (target 5 s)."""
    waited = wait_no_workers(900)
    pooled = pooled_waiting()
    mark = last_event_id()
    r = chat("Reply with just the word: pong")
    done = wait_done(r["item_id"], 120)
    evs = events(after=mark)
    ready = [e for e in evs if e["kind"] == "worker_ready"]
    t = timings(r["item_id"])
    t["worker_start_s"] = round(ready[0]["seconds"], 2) if ready else None
    t["from_pool"] = any(e["kind"] == "assigned" for e in evs)
    t["pooled_waiting_before"] = pooled
    return {"holding": r.get("holding"), "status": done["status"], "reply": (done.get("result") or {}).get("reply"),
            **t, "waited_for_sleep_s": waited,
            "pass": done["status"] == "done" and bool(r.get("holding")) and t.get("answer_s", 99) < 10
            and (t.get("holding_line_ms") or 9999) < 1000}


def t_warm(args) -> dict:
    """A follow-up while the worker is up: under 3 s."""
    r = chat("And again, just: pong")
    done = wait_done(r["item_id"], 60)
    t = timings(r["item_id"])
    return {"status": done["status"], "reply": (done.get("result") or {}).get("reply"), **t,
            "pass": done["status"] == "done" and t.get("warm") and t.get("answer_s", 99) < 3}


def t_attack(args) -> dict:
    """From inside a worker: every attempt outside its policy must fail."""
    r = chat("__attack__", conv="test-attack")
    done = wait_done(r["item_id"], 120)
    res = (done.get("result") or {}).get("attack") or []
    return {"status": done["status"], "results": res,
            "pass": bool(res) and all(x["outcome"] == "ok" for x in res)}


def t_crash(args) -> dict:
    """A worker that dies mid-item: retried, then dead-lettered after 3 tries, and no more workers started for it."""
    r = chat("__crash__", conv="test-crash")
    done = wait_done(r["item_id"], 400)
    evs = events(r["item_id"])
    return {"status": done["status"], "attempts": done.get("attempts"), "error": done.get("error"),
            "events": [e["kind"] for e in evs],
            "pass": done["status"] == "dead" and done.get("attempts") == 3}


def t_burst(args) -> dict:
    """20 schedules due at once: spread by jitter, never more workers than the cap, all done."""
    n = 20
    mark = last_event_id()
    pod(f"""
now = time.time()
for i in range({n}):
    sql("INSERT INTO agent_probe.scheduled_tasks (instruction, created_by, created_by_name, deliver_to, deliver_to_name, run_at, tz, deliver_via, origin) "
        "VALUES (%s, 'test-poc', 'PoC tests', 'test-poc', 'PoC tests', now() - interval '1 second', 'UTC', 'chat', 'poc-burst')",
        f"Reply with just: burst {{i}}")
out = True""")
    t0 = time.monotonic()
    peak, done_n = 0, 0
    while time.monotonic() - t0 < 600:
        peak = max(peak, len(live_workers()))
        rows = pod("out = sql(\"SELECT count(*) FILTER (WHERE status='done'), count(*) FROM frontdoor.work_items "
                   "WHERE kind='schedule' AND payload->'task'->>'instruction' LIKE 'Reply with just: burst%%' "
                   "AND created_at > now() - interval '20 minutes'\")[0]")
        done_n = rows[0]
        if done_n >= n:
            break
        time.sleep(2)
    due = [e for e in events(after=mark) if e["kind"] == "schedule_due"]
    jitter = sorted(e.get("jitter_s", 0) for e in due)
    launched = [e for e in events(after=mark) if e["kind"] == "launched"]
    cap = 2
    return {"done": done_n, "of": n, "seconds": round(time.monotonic() - t0, 1), "peak_workers": peak,
            "workers_launched": len(launched), "jitter_s_min_max": [jitter[0], jitter[-1]] if jitter else None,
            "pass": done_n >= n and peak <= cap}


def t_schedule(args) -> dict:
    """Probe schedules something for itself (through the model) and it fires and is handed for delivery."""
    at = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
    r = chat(f"Schedule a one-off task for {at} (UTC): remind me to stand up and stretch. Use schedule_task now, "
             "no need to confirm.", conv="test-schedule")
    first = wait_done(r["item_id"], 120)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300:
        rows = pod("out = sql(\"SELECT id, status, result FROM frontdoor.work_items WHERE kind='schedule' AND "
                   "payload->'task'->>'instruction' ILIKE '%%stretch%%' ORDER BY id DESC LIMIT 1\")")
        if rows and rows[0][1] in ("done", "dead"):
            res = rows[0][2] or {}
            d = (res.get("deliveries") or [{}])[0]
            return {"scheduled_reply": (first.get("result") or {}).get("reply"), "run_status": rows[0][1],
                    "delivery_to": d.get("user_id"), "delivery_text": (d.get("text") or "")[:200],
                    "pass": rows[0][1] == "done" and bool(d.get("text"))}
        time.sleep(5)
    return {"scheduled_reply": (first.get("result") or {}).get("reply"), "pass": False, "error": "never ran"}


def t_killswitch(args) -> dict:
    """The kill switch stops a running worker within seconds, and nothing is delivered while it is on."""
    r = chat("__sleep 120__", conv="test-kill")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120 and not any(w["state"] == "busy" for w in live_workers()):
        time.sleep(1)
    mark = last_event_id()
    flipped = datetime.now(timezone.utc)
    set_killswitch("stop")
    try:
        t1 = time.monotonic()
        cut = None
        while time.monotonic() - t1 < 60 and cut is None:  # the worker ended: its token revoked, its items back
            ended = [e for e in events(after=mark) if e["kind"] == "worker_ended" and e.get("reason") == "kill switch"]
            if ended:
                cut = round((datetime.fromisoformat(ended[0]["at"]) - flipped).total_seconds(), 1)
            else:
                time.sleep(0.5)
        gone = wait_no_workers(120)
        paused = chat("are you there?", conv="test-kill")
    finally:
        set_killswitch("run")
    return {"cut_off_s": cut, "sandbox_pods_gone_s": round(gone + (time.monotonic() - t1 - gone), 1) if gone else None,
            "while_stopped": paused.get("holding"),
            "pass": cut is not None and cut < 10 and bool(paused.get("paused")),
            "note": "cut off = worker ended and its token revoked (it can no longer deliver anything); includes the "
                    "kill switch poll (5 s)"}


def t_idle(args) -> dict:
    """After the idle time, Probe uses no memory: no worker, no sandbox of its own (the pool's are shared)."""
    t = wait_no_workers(1200)
    return {"seconds_until_zero": t, "pooled_waiting": pooled_waiting(), "sandbox_pods": sandbox_pods(),
            "pass": t is not None}


def t_long(args) -> dict:
    """A task longer than 15 minutes completes (heartbeats keep its lease)."""
    r = chat("__sleep 960__", conv="test-long")
    done = wait_done(r["item_id"], 1300)
    return {"status": done["status"], "attempts": done.get("attempts"), "reply": (done.get("result") or {}).get("reply"),
            "pass": done["status"] == "done" and done.get("attempts") == 1}


def t_poweroff(args) -> dict:
    """A node powered off mid-task loses no work: the item comes back and finishes on another node."""
    if not args.host:
        return {"pass": False, "error": "needs --host USER@HOST (the libvirt host)"}
    r = chat("__sleep 60__", conv="test-poweroff")
    node = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120 and not node:
        busy = [w for w in live_workers() if w["state"] == "busy"]
        if busy:
            out = sh("kubectl", "-n", "sandboxes", "get", "pods", "-o", "wide", "--no-headers", check=False)
            node = next((line.split()[6] for line in out.splitlines() if busy[0]["sandbox"] in line), None)
        time.sleep(1)
    if not node:
        return {"pass": False, "error": "no busy worker found"}
    sh("ssh", args.host, f"sudo virsh destroy {node}")
    killed = time.monotonic()
    try:
        done = wait_done(r["item_id"], 600)
    finally:
        sh("ssh", args.host, f"sudo virsh start {node}", check=False)
    return {"node": node, "status": done["status"], "attempts": done.get("attempts"),
            "recovered_in_s": round(time.monotonic() - killed, 1),
            "pass": done["status"] == "done"}


TESTS = {"cold": t_cold, "warm": t_warm, "attack": t_attack, "crash": t_crash, "burst": t_burst,
         "schedule": t_schedule, "killswitch": t_killswitch, "idle": t_idle, "poweroff": t_poweroff, "long": t_long}
DEFAULT = ["cold", "warm", "attack", "crash", "schedule", "burst", "killswitch", "idle"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tests", nargs="*", default=DEFAULT, choices=list(TESTS))
    ap.add_argument("--host", default=os.environ.get("LIBVIRT_HOST"))
    args = ap.parse_args()
    ok = True
    for name in args.tests:
        print(f"== {name}: {TESTS[name].__doc__}", flush=True)
        try:
            res = TESTS[name](args)
        except Exception as exc:
            res = {"pass": False, "error": f"{type(exc).__name__}: {exc}"}
        ok &= bool(res.get("pass"))
        print(json.dumps(res, indent=1, default=str), flush=True)
        with open(HERE / "poc-results.jsonl", "a") as f:
            f.write(json.dumps({"test": name, "at": datetime.now(timezone.utc).isoformat(), **res}, default=str) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
