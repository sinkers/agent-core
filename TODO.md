# agent-core backlog

## Front door
- [x] **Shared, secure Hindsight** (PoC, 4 Nov): auth proxy that pins each agent's token to its own memory bank; memory on for Probe (docs/design/wake-on-demand.md §7).
- [ ] **Local request preprocessor in the front door** (decided: local, our own; §8). One pass, under ~20 ms on CPU, no network:
  1. *Clean*: Unicode and whitespace; drop quoted reply chains and signatures; strip voice fillers ("um", "uh") and
     fix wake-word and name mishearings ("high probe" → "hi Probe") from a per-agent vocabulary.
  2. *Spell and typo fix*: SymSpell (symspellpy, MIT), with protected terms (people, project codes, product names)
     and nothing inside URLs, code, numbers or quotes. The agent gets the cleaned text and the original is kept in history.
  3. *Basic tool requests*: rules and regexes for reminders, schedules, "stop/cancel", "what time", dates (dateparser).
  4. *Labels from a small local model*: an ONNX sentence embedder (bge-small / MiniLM via fastembed, ~30–100 MB) plus
     small trained heads for intent, complexity (quick / normal / long / not for us) and voice-vs-text style.
     Labels are bootstrapped by having a large model label past conversations offline, then checked by hand.
  5. *Continuation*: same conversation, gap since the last turn, embedding similarity to the last topic, and
     follow-up wording ("and also", "what about", pronouns) → new / continuing / resuming.
  llms2jev (MIT, v0.4.1) was checked and not chosen. It needs a local generative model (torch/transformers or
  Ollama) and scores each candidate answer separately, so it's slower than an embedding head. It only classifies
  (no clean-up or extraction), and its own benchmarks show only modest gains. It could be a fallback judge for ambiguous cases later.
- [ ] **Fast context and greeting**: a contacts key/value store (preferred name, time zone, locale, role, blocked),
  filled from pairing, the Telegram profile and voxhelm. Each finished turn also writes a short topic label.
  Holding lines are built from these: "Good morning Sam, let me get onto that for you"; on a continuation,
  "Back to the octopus question, one moment"; on a long job, "That'll take a few minutes, I'll message you when it's done".
- [ ] Per-agent voxhelm credential instead of an account key (§5.7).
- [ ] Per-client API identities instead of two shared tokens (§5.7).
- [ ] NetworkPolicies for the `agents` and `platform` namespaces (§5.7).
- [ ] Enforce the model spend limit outside the worker (§5.7).
- [ ] Stream answer text from the worker so speech starts on the first sentence.
- [ ] Use voice-gateway's tts_finished (once forwarded to agents) for the exact "listening" moment.
- [ ] Teams and Graph in workers through an egress proxy.
- [ ] memory-proxy in the always-on charts (charts/agent-platform) and an external endpoint for other runtimes.
- [ ] Link a person's channel ids (Telegram, voxhelm, Teams) so private memory follows the person, not the channel.
