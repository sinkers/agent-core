# Agent debug console

Live view of what the agents are doing: each turn (a message, call, email or scheduled task) folds
into one group with its thinking, tool calls and results, model, tokens and cost.

- **Page:** static `index.html`, no build. Publish it with a `config.json` beside it:
  `{"agents": [{"id": "ada", "name": "Ada", "color": "#1B4B8A", "dark": "#6ea8ff"}], "admin": "A. Owner"}`
  (or open it with `?agents=ada,eve`).
- **Stream:** `wss://<page host>/<agent>/debug/ws`, routed to the agent (`agentcore/debug.py`). The agent checks
  the sign-in (a Cognito id token cookie for an internal email) and its viewer list (`DEBUG_VIEWERS`; default the
  agent's owner: the stream holds everything the agent reads).
- **Buffer:** the last 3000 events per agent, in memory only; a restart empties it.
- **Tooling access:** `Authorization: Bearer $INTERNAL_API_TOKEN` on the websocket (no Origin needed).

**If the console says "not connected" for every agent**, check the route from the page's host to each
agent's `/<agent>/debug/ws` and that your account is on the viewer list.
