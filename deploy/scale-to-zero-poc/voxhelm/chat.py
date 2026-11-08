"""Chat with an agent through voxhelm's voice-gateway as a voxhelm app would: open the app socket and send
`chat_message`s, print every reply (`chat_response`). Run inside the cluster (frontdoor.sh voxhelm-chat):

  python3 chat.py ws://voice-gateway.voxhelm.svc.cluster.local:8080/ws poc-user probe "hello" ["follow up" ...]
"""

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import websockets


async def main(url: str, user: str, agent: str, texts: list[str]) -> None:
    q = urlencode({"user_id": user, "agent_id": agent, "client_type": "app"})
    async with websockets.connect(f"{url}?{q}") as ws:
        for text in texts:
            t0 = time.monotonic()
            await ws.send(json.dumps({"type": "chat_message", "id": str(uuid.uuid4()),
                                      "timestamp": datetime.now(timezone.utc).isoformat(), "payload": {"text": text}}))
            print(f">>> {text}", flush=True)
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if msg.get("type") != "chat_response":
                    continue
                p = msg.get("payload") or {}
                holding = (p.get("metadata") or {}).get("holding")
                print(f"<<< [{time.monotonic() - t0:5.1f} s{' holding' if holding else ''}] {p.get('text')}", flush=True)
                if not holding:
                    break


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]))
