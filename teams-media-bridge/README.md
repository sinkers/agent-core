# teams-media-bridge

A thin C# relay that gives the agent a voice in Microsoft Teams calls. It answers Teams calls to the trial bot
(and, in phase 2, joins meetings by link), then pipes audio both ways between Teams and an
**ElevenLabs Agents** conversation over WebSocket. The bridge does no turn detection, barge-in logic, STT,
TTS or LLM work. ElevenLabs does all of that, and the agent (Python, Linux) sets up each conversation.

Teams real-time audio is only available to Graph **application-hosted media** bots, and those run only on
Windows with .NET. That is the only reason this component exists and why it is kept small.

## Architecture

```
 Teams client ──call──▶ Teams / Graph ──HTTPS 443 /api/calling──────────────▶ ┌──────────────────────────┐
                              ▲  ▲                                            │ TeamsMediaBridge (VM)    │
                              │  └──── TCP 8445 real-time media (PCM16K) ◀──▶ │  BotService (Graph SDK)  │
                              │                                               │  CallSession per call    │
                              └── Graph REST (answer, hang up, keepalive) ◀── │   ├ AudioPlayout 20 ms   │
                                                                              │   └ ElevenLabsConnection │
 the agent (Linux) ◀── GET /ada/voice/session?call_id&caller_aad_id&… ──────── │                          │
   returns {signed_url, conversation_initiation_client_data}                  └────────────┬─────────────┘
                                                                                           │ wss (signed_url)
                                                                        ElevenLabs Agents ◀┘
```

Per call:

1. Graph POSTs the incoming call to `/api/calling`. The bridge answers with app-hosted media:
   audio only, `AudioSocket` Pcm16K, send+receive, one audio stream (1:1 calls are always one stream; meetings
   are below).
   Calls over `Bot:MaxConcurrentCalls` are rejected as Busy.
2. It reads the caller's AAD id and display name from the call's `source.identity.user`.
3. `GET {Agent:SessionUrl}?call_id=…&caller_aad_id=…&caller_name=…&kind=p2p` with
   `Authorization: Bearer {Agent:BridgeToken}`. The agent replies
   `{"signed_url":"wss://…","conversation_initiation_client_data":{…}}`.
   the agent holds the ElevenLabs API key and agent id. The bridge only ever sees the short-lived signed URL.
4. It opens the WebSocket and sends `conversation_initiation_client_data` first, adding
   `"type":"conversation_initiation_client_data"` if it is missing. This starts while Teams is still setting up
   media, so the agent's greeting is queued and plays as soon as the socket reports send-active.
5. **Teams → agent**: each 20 ms, 640-byte frame is copied off the media thread into a bounded queue. Every
   `Relay:FramesPerSend` frames (default 5, i.e. 100 ms) it sends `{"user_audio_chunk":"<base64>"}`.
6. **Agent → Teams**: `audio` events are base64-decoded and cut into exact 640-byte frames. The tail is padded
   with silence once audio stops. A dedicated 20 ms timer thread keeps the AudioSocket's own send queue
   topped up to `Relay:SocketQueueFrames` frames (default 3).
   - `interruption`: the playback queue is cleared at once. At most `SocketQueueFrames × 20 ms` (60 ms) that
     is already inside the socket can still play. Late `audio` chunks with `event_id` below the interruption's
     `event_id` are dropped, the same rule the ElevenLabs JS client uses.
   - `ping`: the bridge replies `{"type":"pong","event_id":N}` straight away.
   - `user_transcript` and `agent_response` are logged at Info.
7. **Teardown**: when the call ends, the bridge closes the WebSocket. If the WebSocket closes first (the agent's
   end_call tool, an error or a drop), the bridge waits up to `Relay:GoodbyeDrainSeconds` for queued audio to
   finish, then hangs up (`DELETE` call). Calls are kept alive with `KeepAliveAsync` every 10 minutes.

### Meetings (phase 2)

`POST /api/join` with `Authorization: Bearer {Agent:BridgeToken}` and body
`{"joinUrl":"https://teams.microsoft.com/l/meetup-join/…","requestedBy":"<aad id>"}` joins the meeting with
the same media settings. The relay then runs with `kind=meeting` and `caller_aad_id=<requestedBy>`, and
returns `202 {"callId":"…"}`.

- Classic `meetup-join` links are parsed as the EchoBot sample does (thread id, message id, `Tid`/`Oid` context).
- Short `teams.microsoft.com/meet/<id>?p=<passcode>` links map to `JoinMeetingIdMeetingInfo`. This is
  **untested** (marked TODO).
- Joining as a guest (display-name override, which lands in the lobby) is not implemented.

#### Who is speaking

Meeting joins set `ReceiveUnmixedMeetingAudio = true`: each 20 ms frame also carries up to four per-speaker
streams (`UnmixedAudioBuffers`, the top four active speakers, each tagged with its media source id, MSI). In
that mode the platform sends *only* unmixed audio, so `EnableLocalAudioMixingForUnmixed = true` has the SDK
mix them into the frame's `Data` as well (this also turns on packet-loss healing). ElevenLabs still gets one
stream. If `Data` ever arrives empty, the bridge sums the per-speaker PCM itself (clipped to int16). If no frame
arrives for 100 ms, it sends 100 ms of silence so ElevenLabs' turn-taking sees speech stop. 1:1 calls are
unchanged.

- **Voice activity**: per speaker, RMS ≥ `Relay:SpeechRmsThreshold` (default 500, about −36 dBFS) counts as
  speech, plus a `Relay:SpeechHangoverMs` (200 ms) hangover. Without per-speaker streams, the frame's
  `ActiveSpeakers` list is used instead.
- **MSI → person**: rebuilt from the roster on every participant update (joins and leaves). The MSI is the
  `SourceId` of each participant's audio media stream; the person is `info.identity` (`user` id and name for
  Entra users; guests and phone callers get a name and an empty `aad_id`).
- **Reports**: every `Relay:SpeakerWindowMs` (500 ms), if anyone spoke in the window, the bridge POSTs to
  `{Agent:SessionUrl}` with the same bearer token:

  ```json
  {"type":"speakers","call_id":"<Graph call id, as sent on the session GET>","at_ms":1760486400500,
   "window_ms":500,"speakers":[{"msi":123,"aad_id":"<guid or empty>","name":"<name or empty>","speech_ms":340}]}
  ```

  `at_ms` is Unix ms at the window's end. Speakers are sorted by `speech_ms`, highest first.
  `speech_ms` is capped at `window_ms`. After speech, once everyone has been quiet for
  `Relay:SpeakerSilenceMs` (1.5 s), one report with `"speakers":[]` is sent; it is sent once per silence. Posting
  runs off the media thread through an 8-deep queue: if the agent is slow or down, reports are dropped, never
  queued up. Drops are logged at most once a minute. Reports start when the agent starts (someone is present).

### Reconnect (fresh agent conversation, same call)

`POST /api/voice/reconnect` with `Authorization: Bearer {Agent:BridgeToken}` and body `{"call_id":"<id>"}`.
`id` is the `call_id` the bridge sent on the session GET (the Graph call id); for outbound calls the agent's `ref`
works too. The bridge:

1. closes the call's ElevenLabs WebSocket cleanly (close frame; it is aborted if the agent hasn't answered
   within 3 s);
2. requests a new session with the same `GET {Agent:SessionUrl}?…` plus `reconnect=1`, retried twice;
3. connects the new conversation.

The Teams call stays up and the meeting gate is untouched, so the "people present" start doesn't fire again.
Agent audio already queued keeps playing, and caller audio from the gap goes to the new conversation. If the new
session can't be had after the retries, the call ends as it would after any agent failure.

| Response | When |
|---|---|
| `202 {"reconnected":true}` | Call found; the swap runs in the background |
| `404 {"reconnected":false,"error":"no such call"}` | No active call with that id |
| `409 {"reconnected":false,"error":"agent not started yet"}` | A meeting still waiting for people; nothing to swap |
| `401` | Wrong or missing bearer token |

Any greeting is the agent's choice: with `reconnect=1` it should return initiation data without a first message.

## Endpoints and ports

| Port | Proto | Purpose | Who connects |
|---|---|---|---|
| 443 (`Bot:CallSignalingPort`) | HTTPS (Kestrel, CA cert) | `POST /api/calling` (Azure Bot calling webhook), `POST /api/calling/notification` (per-call notifications), `GET /healthz`, `POST /api/join`, `POST /api/call`, `POST /api/voice/reconnect` | Microsoft Graph; the agent (join, call, reconnect); probes |
| 8445 (`Bot:InstancePublicPort`/`InstanceInternalPort`) | TCP | Real-time media platform | Teams media servers (Azure). The EchoBot template limits the source to the `AzureCloud` service tag |
| 80 | HTTP | Let's Encrypt HTTP-01 validation, served by win-acme itself only while it validates (issue and renewal) | Let's Encrypt |

`GET /healthz` returns `200 {"status":"ok","activeCalls":N}`.

## Configuration

Settings load in this order, and later sources win:

1. `appsettings.json`
2. `appsettings.{Environment}.json`
3. `BRIDGE_CONFIG` (default `C:\teams-media-bridge\config\bridge.json`, written by `install.ps1` and readable
   only by SYSTEM and Administrators)
4. Environment variables (`Bot__AppSecret`, …)

There are no secrets in the repo. See `appsettings.example.json`.

| Key | Default | Notes |
|---|---|---|
| `Bot:AppId` | — | Single-tenant Entra app (also the Azure Bot's app id) |
| `Bot:AppSecret` | — | Client secret. **Secret** |
| `Bot:TenantId` | — | Token authority; also used for meeting links without a tenant |
| `Bot:ServiceFqdn` | — | Public DNS name of the VM. It must resolve to the VM's public IP and match the cert |
| `Bot:CertificateThumbprint` | — | Cert in `LocalMachine\My`. If it is missing, the newest valid cert for `ServiceFqdn` is used and a warning is logged |
| `Bot:InstancePublicPort` / `Bot:InstanceInternalPort` | 8445 | Media TCP port |
| `Bot:CallSignalingPort` | 443 | HTTPS listener |
| `Bot:MaxConcurrentCalls` | 4 | Calls over this are rejected Busy |
| `Agent:SessionUrl` | — | e.g. `https://agents.example.net/ada/voice/session` |
| `Agent:BridgeToken` | — | Bearer token, 16 characters or more. It is sent to the agent and required on `/api/join`. **Secret** |
| `Relay:FramesPerSend` | 5 | 20 ms frames per `user_audio_chunk` |
| `Relay:SocketQueueFrames` | 3 | Jitter buffer inside the AudioSocket. This is also the most stale audio that can play after a barge-in |
| `Relay:UplinkBufferFrames` | 100 | Caller audio held while the agent socket connects. The oldest is dropped first |
| `Relay:GoodbyeDrainSeconds` | 10 | How long to let queued audio play before hanging up |
| `Relay:SpeechRmsThreshold` | 500 | Meetings: per-speaker RMS (PCM16) that counts as speech |
| `Relay:SpeechHangoverMs` | 200 | Meetings: a speaker still counts as talking this long after their last loud frame |
| `Relay:SpeakerWindowMs` | 500 | Meetings: speakers report interval |
| `Relay:SpeakerSilenceMs` | 1500 | Meetings: quiet after speech before the one empty speakers report |
| `Logs:Directory` | `C:\teams-media-bridge\logs` | Daily rolling `bridge-YYYYMMDD.log`, kept 14 days, plus console |

Latency markers are logged with a `[latency]` prefix:

- call established after answer
- first non-silent caller audio after established
- agent socket open
- first `user_audio_chunk` sent
- each agent audio start (greeting, and ms after the last user transcript)
- each interruption, with the ms of audio dropped

### ElevenLabs agent requirements

- **Input and output audio must both be PCM 16000 Hz** (agent Voice tab and Advanced tab). A warning is logged
  if `conversation_initiation_metadata` reports anything else.
- The client events `audio`, `interruption`, `ping`, `user_transcript` and `agent_response` must be enabled.
- If the agent sends `conversation_config_override` or `dynamic_variables` in the initiation data, the agent's
  security settings must allow those overrides.

## Infrastructure the VM needs

- Windows Server 2022 on Azure, x64, with **at least 2 physical cores / 4 vCPU** (e.g. `Standard_D4s_v3`).
  Region Australia East.
- A **Standard static public IP on the NIC** (instance-level public IP, no load balancer) and a DNS label, so
  that `ServiceFqdn` resolves to it.
- NSG inbound rules: TCP 80, 443 and 8445.
- The Entra app needs the Graph application permissions `Calls.AccessMedia.All` and `Calls.JoinGroupCall.All`
  (for meetings), plus `Calls.Initiate.All` if the bot will ever place calls. Admin consent is required.
- An Azure Bot (single-tenant, same app id) with the Teams channel's calling enabled. The webhook is
  `https://<fqdn>/api/calling`.
- A Teams app manifest with `"supportsCalling": true` for the bot.
- Outbound access to `graph.microsoft.com`, `login.microsoftonline.com`, the ElevenLabs API
  (`api.elevenlabs.io`, or the residency host in `signed_url`) and the **agent session URL**. That URL is a
  Tailscale `*.ts.net` name, so the VM must be on the tailnet (Tailscale for Windows) or the agent must publish
  it through Funnel.

## Deploy

```bash
./deploy/publish.sh          # → teams-media-bridge.zip (win-x64, framework-dependent)
# upload the zip to blob storage and make a short-lived read SAS URL, then:
az vm run-command create -g <rg> --vm-name <vm> --name install-bridge --async-execution false \
  --script @deploy/install.ps1 \
  --parameters PackageUrl=<sas-url> Fqdn=<fqdn> Email=<email> AppId=<id> TenantId=<tid> \
               AgentSessionUrl=https://agents.example.net/ada/voice/session \
  --protected-parameters AppSecret=<secret> AgentBridgeToken=<token>
```

`install.ps1` is idempotent. It does the following:

1. Installs the .NET 8 runtime, the VC++ redistributable and Server-Media-Foundation. It warns if a reboot
   is needed.
2. Unpacks the zip to `C:\teams-media-bridge\app`.
3. Opens the firewall ports.
4. Installs win-acme, obtains the Let's Encrypt cert, and registers a renewal hook (`update-cert.ps1`). The hook
   rewrites the thumbprint and restarts the service when no calls are active.
5. Writes `config\bridge.json` and registers the `TeamsMediaBridge` service (LocalSystem, automatic, restart
   on failure).
6. Checks `/healthz`.

Pass `-CertificateThumbprint` to use an existing cert instead of win-acme. Secrets are never printed.

## Build and test locally (macOS/Linux)

```bash
~/.dotnet/dotnet build TeamsMediaBridge.sln
~/.dotnet/dotnet test tests/TeamsMediaBridge.Tests
```

The app compiles anywhere but **runs only on Windows x64**, because the media platform
(`Microsoft.Skype.Bots.Media`) is native Windows code. `Program.cs` exits at once on other operating systems.
The tests cover the platform-independent parts:

- frame slicing, padding and barge-in clearing (`FrameQueue`)
- the agent session request and response handling
- join-URL parsing
- the bearer check

## Known limits and gotchas

- **SDK freshness.** Microsoft requires `Microsoft.Graph.Communications.Calls.Media` to be the newest version
  or no more than **3 months old**; older builds stop working. This build pins **1.2.0.18736** (released
  28 Sep 2026), which pulls `Microsoft.Skype.Bots.Media` 1.33.0.258-preview. Bump all
  `Microsoft.Graph.Communications.*` packages together at least quarterly.
  See [requirements for app-hosted media bots](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots).
- **Production support is Azure only.** On-premises Windows is allowed for dev and test only.
- **Calls are pinned to the VM** that answered them. Stopping or redeploying the service or VM drops active
  calls. `install.ps1` stops the service before unpacking. There is one VM and no failover.
- Media is audio only. Meetings get per-speaker streams for who-is-speaking, but ElevenLabs still hears one mix,
  so its transcript has no speaker labels. The agent lines up the speakers reports with the transcript by time.
- Per-speaker audio covers the **top four** active speakers at a time. The speech threshold of 500 RMS is a
  starting value, not yet tuned on real meetings.
- The trial auth uses a client secret. A certificate credential would be better for production
  (`MsalTokenProvider` accepts any MSAL confidential client).

## References

- ElevenLabs, Graph media bot for Teams (the base for this bridge): https://elevenlabs.io/docs/eleven-agents/phone-numbers/microsoft-teams/graph-media-bot
- ElevenLabs Agents WebSocket API: https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket
- Microsoft Graph comms samples (EchoBot, AudioVideoPlaybackBot): https://github.com/microsoftgraph/microsoft-graph-comms-samples
- App-hosted media requirements: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots
- win-acme CLI: https://www.win-acme.com/reference/cli
