using System.Buffers;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading.Channels;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;
using TeamsMediaBridge.Bot;

namespace TeamsMediaBridge.Relay;

/// <summary>
/// One Teams call ⇄ one ElevenLabs conversation (swappable for a fresh one, see <see cref="RequestReconnect"/>).
/// Owns: the the agent session lookup, the WebSocket, the uplink batching loop, the playout pacer and the Graph
/// keep-alive; in meetings also who-is-speaking (per-speaker streams → SpeakerTracker → POST to the agent).
/// No speech logic lives here.
/// </summary>
public sealed class CallSession : IAgentEvents, IAsyncDisposable
{
    private readonly ICall _call;
    private readonly string _kind;
    private readonly string? _callerAadId;
    private readonly string? _callerName;
    private readonly string? _callRef;
    private readonly AgentSessionClient _agentClient;
    private readonly RelayOptions _options;
    private readonly ILogger _logger;
    private readonly CancellationTokenSource _cts = new();
    private readonly System.Threading.Channels.Channel<byte[]> _uplink;
    private readonly IAudioSocket _socket;
    private readonly AudioPlayout _playout;
    private readonly Timer _keepAlive;
    private readonly long _createdAt = Stopwatch.GetTimestamp();

    private const int FrameMs = 20;

    private volatile ElevenLabsConnection? _agent;
    private CancellationTokenSource? _agentCts;
    private int _reconnectPending;
    private Task _relayTask = Task.CompletedTask;

    // Meetings only: who is speaking, reported to the agent every SpeakerWindowMs off the media thread.
    private readonly SpeakerTracker? _speakers;
    private readonly SpeakerRoster _roster = new();
    private readonly System.Threading.Channels.Channel<SpeakersReport>? _reports;
    private Task _reportTask = Task.CompletedTask;
    private Timer? _speakerTimer;
    private Timer? _silencePump;
    private long _lastFrameAt;
    private int _reportFailures;
    private long _lastReportWarnAt;
    private int _disposed;
    private int _relayStarted;
    private readonly MeetingGate? _gate;
    private Timer? _gateTimer;
    private long _establishedAt;
    private long _lastUserTranscriptAt;
    private bool _firstCallerAudioLogged;
    private bool _firstUplinkLogged;
    private long _lastInterruptionEventId = long.MinValue;

    public CallSession(ICall call, string kind, string? callerAadId, string? callerName,
        AgentSessionClient agentClient, RelayOptions options, ILogger logger, string? callRef = null)
    {
        _callRef = callRef;
        _call = call;
        _kind = kind;
        _callerAadId = callerAadId;
        _callerName = callerName;
        _agentClient = agentClient;
        _options = options;
        _logger = logger;

        // Bounded + DropOldest: if the agent socket is slow or still connecting we lose old audio, never block
        // the media platform's callback thread.
        _uplink = System.Threading.Channels.Channel.CreateBounded<byte[]>(new BoundedChannelOptions(options.UplinkBufferFrames)
        {
            FullMode = BoundedChannelFullMode.DropOldest,
            SingleReader = true,
            SingleWriter = false, // media thread, plus the meeting silence pump
        });

        _socket = call.GetLocalMediaSession().AudioSocket
            ?? throw new InvalidOperationException("Call has no local audio socket (not app-hosted media?)");
        _playout = new AudioPlayout(_socket, options.SocketQueueFrames, logger);
        _playout.UtteranceStarted += OnUtteranceStarted;

        // Graph ends calls it has not heard from for 45 min; the samples send KeepAlive every 10 min
        // (EchoBot CallHandler: HeartbeatHandler(TimeSpan.FromMinutes(10))).
        _keepAlive = new Timer(_ => _ = KeepAliveAsync(), null, TimeSpan.FromMinutes(10), TimeSpan.FromMinutes(10));

        if (IsMeeting)
        {
            _gate = new MeetingGate(DateTimeOffset.UtcNow, TimeSpan.FromMinutes(options.MeetingWaitForPeopleMinutes),
                TimeSpan.FromSeconds(options.MeetingAloneLeaveSeconds));
            _speakers = new SpeakerTracker(options.SpeechRmsThreshold, options.SpeechHangoverMs, options.SpeakerWindowMs, options.SpeakerSilenceMs);
            _reports = System.Threading.Channels.Channel.CreateBounded<SpeakersReport>(new BoundedChannelOptions(8)
            {
                FullMode = BoundedChannelFullMode.DropWrite, // The agent slow or down: lose reports, never queue up
                SingleReader = true,
                SingleWriter = true,
            });
        }
    }

    public string CallId => _call.Id;

    public string Kind => _kind;

    /// <summary>the agent's own id for an outbound call (POST /api/call ref), else null.</summary>
    public string? CallRef => _callRef;

    public void Start()
    {
        _socket.AudioMediaReceived += OnAudioMediaReceived;
        _call.OnUpdated += OnCallUpdated;
        _logger.LogInformation("Call {CallId} ({Kind}) from {CallerName} ({CallerAadId}) — starting relay",
            CallId, _kind, _callerName ?? "?", _callerAadId ?? "?");
        // 1:1 calls: connect to the agent while Teams is still setting up media; the greeting is queued and plays
        // as soon as the socket reports send-active. Outbound calls start when the person answers, meetings when
        // a person is in the meeting (OnCallUpdated / EvaluateMeeting): no greeting into an empty room.
        if (IsMeeting)
        {
            RefreshRoster();
            _call.Participants.OnUpdated += OnParticipantsUpdated;
            _gateTimer = new Timer(_ => EvaluateMeeting(), null, TimeSpan.FromSeconds(15), TimeSpan.FromSeconds(15));
        }
        else if (!IsOutbound)
        {
            StartRelay();
        }
    }

    /// <summary>Calls the agent placed (POST /api/call) rather than answered.</summary>
    public bool IsOutbound => _kind == "teams-out";

    public bool IsMeeting => _kind == "meeting";

    private void StartRelay()
    {
        if (Interlocked.Exchange(ref _relayStarted, 1) == 0)
        {
            _relayTask = Task.Run(RunRelayAsync);
            if (_speakers is not null && _reports is not null)
            {
                Volatile.Write(ref _lastFrameAt, Environment.TickCount64);
                _reportTask = Task.Run(() => PostReportsAsync(_reports.Reader, _cts.Token));
                var window = TimeSpan.FromMilliseconds(_speakers.WindowMs);
                _speakerTimer = new Timer(_ => ReportSpeakers(), null, window, window);
                _silencePump = new Timer(_ => PumpSilence(), null, TimeSpan.FromMilliseconds(100), TimeSpan.FromMilliseconds(100));
            }
        }
    }

    private void OnParticipantsUpdated(IParticipantCollection sender, CollectionEventArgs<IParticipant> args)
    {
        RefreshRoster();
        EvaluateMeeting();
    }

    /// <summary>Rebuild MSI → person from the roster (joins and leaves both arrive as participant updates).</summary>
    private void RefreshRoster()
    {
        try
        {
            _roster.Update(_call.Participants.Select(p => p.Resource));
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "Roster refresh failed for call {CallId}", CallId);
        }
    }

    private void EvaluateMeeting()
    {
        if (_gate is null || _disposed == 1 || _establishedAt == 0)
        {
            return;
        }

        try
        {
            var people = MeetingGate.CountPeople(_call.Participants.Select(p => p.Resource));
            switch (_gate.Update(people, DateTimeOffset.UtcNow))
            {
                case GateAction.StartAgent:
                    _logger.LogInformation("Meeting {CallId}: {People} people present; starting agent", CallId, people);
                    StartRelay();
                    break;
                case GateAction.Leave:
                    _logger.LogInformation("Meeting {CallId}: {Why}; leaving", CallId,
                        _gate.AgentStarted ? "everyone else has left" : "nobody joined");
                    _call.DeleteAsync().Forget(_logger, $"Leaving {CallId}");
                    break;
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Meeting presence check failed for call {CallId}", CallId);
        }
    }

    // ---------- Teams side ----------

    private void OnCallUpdated(ICall sender, ResourceEventArgs<Call> e)
    {
        var state = e.NewResource.State;
        if (state == e.OldResource.State)
        {
            return;
        }

        _logger.LogInformation("Call {CallId} state {State} {Result}", CallId, state, e.NewResource.ResultInfo?.Message);
        if (state == CallState.Established)
        {
            _establishedAt = Stopwatch.GetTimestamp();
            _logger.LogInformation("[latency] call {CallId} established {Ms} ms after answer", CallId, Ms(_createdAt));
            if (IsOutbound)
            {
                StartRelay();
            }
            else if (IsMeeting)
            {
                EvaluateMeeting();
            }
        }
        else if (state == CallState.Terminated)
        {
            _ = DisposeAsync().AsTask();
        }
    }

    /// <summary>
    /// Media platform callback: one ~20 ms frame of PCM16K caller audio. Must return quickly and the buffer MUST be
    /// disposed (it is unmanaged memory owned by the platform). In meetings the frame also carries up to four
    /// per-speaker streams: each feeds the speaker tracker, and if the SDK's local mix (Data) is ever missing they
    /// are mixed here so ElevenLabs still hears one stream.
    /// </summary>
    private void OnAudioMediaReceived(object? sender, AudioMediaReceivedEventArgs e)
    {
        try
        {
            if (_cts.IsCancellationRequested || _relayStarted == 0)
            {
                return;
            }

            var buffer = e.Buffer;
            var length = (int)buffer.Length;
            byte[]? pcm = null;
            if (length > 0 && buffer.Data != IntPtr.Zero)
            {
                pcm = new byte[length];
                Marshal.Copy(buffer.Data, pcm, 0, length);
            }

            if (_speakers is not null)
            {
                pcm = TrackSpeakers(buffer, pcm);
            }

            if (pcm is null)
            {
                return;
            }

            Volatile.Write(ref _lastFrameAt, Environment.TickCount64);
            _uplink.Writer.TryWrite(pcm);

            if (!_firstCallerAudioLogged && !buffer.IsSilence && _establishedAt != 0)
            {
                _firstCallerAudioLogged = true;
                _logger.LogInformation("[latency] call {CallId} first non-silent caller audio {Ms} ms after established",
                    CallId, Ms(_establishedAt));
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Audio receive failed on call {CallId}", CallId);
        }
        finally
        {
            e.Buffer.Dispose();
        }
    }

    /// <summary>Feed the speaker tracker from one meeting frame; returns the frame to send (mixing if needed).</summary>
    private byte[]? TrackSpeakers(AudioMediaBuffer buffer, byte[]? mixed)
    {
        var now = Environment.TickCount64;
        var unmixed = buffer.UnmixedAudioBuffers;
        if (unmixed is not null && unmixed.Length > 0)
        {
            var streams = mixed is null ? new List<byte[]>(unmixed.Length) : null;
            foreach (var u in unmixed)
            {
                var n = (int)u.Length;
                if (n <= 0 || u.Data == IntPtr.Zero)
                {
                    continue;
                }

                var one = new byte[n];
                Marshal.Copy(u.Data, one, 0, n);
                _speakers!.AddLevel(u.ActiveSpeakerId, Pcm16.Rms(one), FrameMs, now);
                streams?.Add(one);
            }

            if (streams is { Count: > 0 })
            {
                mixed = Pcm16.Mix(streams, AudioPlayout.FrameBytes);
            }
        }
        else if (!buffer.IsSilence && buffer.ActiveSpeakers is { Length: > 0 } active)
        {
            // Fallback when no per-speaker streams came: the platform's active-speaker list for the mix.
            _speakers!.AddActive(active, FrameMs, now);
        }

        return mixed;
    }

    /// <summary>
    /// Meetings: if no frame has arrived for 100 ms (the docs don't say whether unmixed mode raises frames during
    /// silence), send 100 ms of silence so ElevenLabs' turn-taking still sees the speaker stop.
    /// </summary>
    private void PumpSilence()
    {
        var now = Environment.TickCount64;
        if (_cts.IsCancellationRequested || now - Volatile.Read(ref _lastFrameAt) < 100)
        {
            return;
        }

        Volatile.Write(ref _lastFrameAt, now);
        for (var i = 0; i < 5; i++)
        {
            _uplink.Writer.TryWrite(new byte[AudioPlayout.FrameBytes]);
        }
    }

    /// <summary>Timer: close the speaker window and queue the report (if any) for the agent. Never blocks.</summary>
    private void ReportSpeakers()
    {
        try
        {
            if (_cts.IsCancellationRequested || _speakers is null || _reports is null)
            {
                return;
            }

            var report = _speakers.Flush(CallId, Environment.TickCount64, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), _roster);
            if (report is null)
            {
                return;
            }

            if (SpeakerTracker.HasUnknown(report))
            {
                RefreshRoster(); // someone's stream arrived before their roster update
                report = SpeakerTracker.Resolve(report, _roster);
            }

            if (!_reports.Writer.TryWrite(report))
            {
                NoteReportFailure("queue full");
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "Speaker report failed for call {CallId}", CallId);
        }
    }

    private async Task PostReportsAsync(ChannelReader<SpeakersReport> reader, CancellationToken ct)
    {
        try
        {
            await foreach (var report in reader.ReadAllAsync(ct).ConfigureAwait(false))
            {
                if (!await _agentClient.PostSpeakersAsync(report, ct).ConfigureAwait(false))
                {
                    NoteReportFailure("POST failed");
                }
            }
        }
        catch (OperationCanceledException)
        {
            // call ended
        }
    }

    /// <summary>Count dropped speaker reports; log at most once a minute.</summary>
    private void NoteReportFailure(string why)
    {
        var failures = Interlocked.Increment(ref _reportFailures);
        var now = Environment.TickCount64;
        var last = Interlocked.Read(ref _lastReportWarnAt);
        if ((last == 0 || now - last >= 60_000) && Interlocked.CompareExchange(ref _lastReportWarnAt, now, last) == last)
        {
            _logger.LogWarning("Call {CallId}: speaker reports to the agent dropped ({Why}; {Count} so far)", CallId, why, failures);
        }
    }

    private async Task KeepAliveAsync()
    {
        try
        {
            await _call.KeepAliveAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "KeepAlive failed for call {CallId}", CallId);
        }
    }

    // ---------- Agent side ----------

    private async Task RunRelayAsync()
    {
        var ct = _cts.Token;
        string reason;
        var reconnect = false;
        try
        {
            while (true)
            {
                var session = await GetSessionAsync(reconnect, ct).ConfigureAwait(false);
                var agentCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                var agent = new ElevenLabsConnection(_logger);
                _lastInterruptionEventId = long.MinValue; // event ids start again with each conversation
                _agentCts = agentCts;
                _agent = agent;
                await agent.ConnectAsync(session.SignedUrl, session.InitiationData, ct).ConfigureAwait(false);
                _logger.LogInformation("[latency] call {CallId} agent socket open {Ms} ms after answer{Again}", CallId, Ms(_createdAt),
                    reconnect ? " (reconnected)" : "");

                var uplink = Task.Run(() => UplinkLoopAsync(agent, agentCts.Token), CancellationToken.None);
                try
                {
                    reason = await agent.RunReceiveLoopAsync(this, agentCts.Token).ConfigureAwait(false);
                }
                catch (Exception ex) when (_reconnectPending == 1 && !ct.IsCancellationRequested)
                {
                    reason = "closed for reconnect: " + ex.Message;
                }

                if (Interlocked.Exchange(ref _reconnectPending, 0) == 1 && !ct.IsCancellationRequested)
                {
                    // Swap: stop this uplink reader before the next one starts (single reader), close and free the
                    // old socket. Queued agent audio keeps playing; caller audio queued meanwhile goes to the new one.
                    agentCts.Cancel();
                    await uplink.ConfigureAwait(false);
                    _agent = null;
                    _agentCts = null;
                    await agent.DisposeAsync().ConfigureAwait(false);
                    agentCts.Dispose();
                    _logger.LogInformation("Call {CallId}: agent conversation closed ({Reason}); reconnecting", CallId, reason);
                    reconnect = true;
                    continue;
                }

                _uplink.Writer.TryComplete();
                await Task.WhenAny(uplink, Task.Delay(1000, CancellationToken.None)).ConfigureAwait(false);
                break;
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return; // call ended first; DisposeAsync handles the rest
        }
        catch (Exception ex)
        {
            reason = "error: " + ex.Message;
            _logger.LogError(ex, "Relay failed for call {CallId}", CallId);
        }

        if (ct.IsCancellationRequested)
        {
            return;
        }

        // Agent side ended (end_call tool, error, or socket drop): let the goodbye finish, then hang up politely.
        _logger.LogInformation("Agent conversation for call {CallId} ended ({Reason}); hanging up", CallId, reason);
        _ = _agentClient.ReportEndAsync(CallId, _kind, reason, Stopwatch.GetElapsedTime(_createdAt).TotalSeconds);
        var deadline = Stopwatch.GetTimestamp() + (Stopwatch.Frequency * _options.GoodbyeDrainSeconds);
        while (_playout.QueuedMs > 0 && Stopwatch.GetTimestamp() < deadline && !ct.IsCancellationRequested)
        {
            await Task.Delay(100, CancellationToken.None).ConfigureAwait(false);
        }

        await Task.Delay((_options.SocketQueueFrames * 20) + 200, CancellationToken.None).ConfigureAwait(false);
        try
        {
            await _call.DeleteAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Hang-up failed for call {CallId}", CallId);
        }
    }

    /// <summary>A session from the agent; a reconnect (reconnect=1) retries twice before giving up on the call.</summary>
    private async Task<AgentSession> GetSessionAsync(bool reconnect, CancellationToken ct)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                return await _agentClient.GetSessionAsync(CallId, _callerAadId, _callerName, _kind, ct, _callRef, reconnect).ConfigureAwait(false);
            }
            catch (Exception ex) when (reconnect && attempt < 3 && !ct.IsCancellationRequested)
            {
                _logger.LogWarning("Call {CallId}: reconnect session request failed ({Error}); retrying", CallId, ex.Message);
                await Task.Delay(1000 * attempt, ct).ConfigureAwait(false);
            }
        }
    }

    /// <summary>
    /// POST /api/voice/reconnect: close the current ElevenLabs conversation cleanly and open a fresh one from the agent
    /// (same session request plus reconnect=1). The Teams call, the meeting gate and the playout are untouched, so
    /// there is no hang-up and no second "people present" start; callers hear a short silence.
    /// </summary>
    public ReconnectResult RequestReconnect()
    {
        if (_disposed == 1 || _cts.IsCancellationRequested)
        {
            return ReconnectResult.NotFound;
        }

        if (_relayStarted == 0)
        {
            return ReconnectResult.NotStarted;
        }

        var agent = _agent;
        if (agent is null)
        {
            return ReconnectResult.Accepted; // a session is being fetched right now: it is already a fresh one
        }

        if (Interlocked.Exchange(ref _reconnectPending, 1) == 0)
        {
            _logger.LogInformation("Call {CallId}: reconnect requested", CallId);
            _ = CloseForReconnectAsync(agent);
        }

        return ReconnectResult.Accepted;
    }

    private async Task CloseForReconnectAsync(ElevenLabsConnection agent)
    {
        try
        {
            await agent.CloseAsync("reconnect").ConfigureAwait(false);
            // The receive loop ends when the agent answers our close frame; don't wait on it forever.
            await Task.Delay(3000, _cts.Token).ConfigureAwait(false);
            if (ReferenceEquals(_agent, agent) && _reconnectPending == 1)
            {
                _agentCts?.Cancel();
            }
        }
        catch (Exception ex) when (ex is OperationCanceledException or ObjectDisposedException)
        {
            // call ended, or the swap already finished
        }
    }

    /// <summary>Batch FramesPerSend Teams frames (default 5 × 20 ms = 100 ms) per user_audio_chunk.</summary>
    private async Task UplinkLoopAsync(ElevenLabsConnection agent, CancellationToken ct)
    {
        var batch = new ArrayBufferWriter<byte>(_options.FramesPerSend * AudioPlayout.FrameBytes);
        var frames = 0;
        try
        {
            await foreach (var frame in _uplink.Reader.ReadAllAsync(ct).ConfigureAwait(false))
            {
                batch.Write(frame);
                if (++frames < _options.FramesPerSend)
                {
                    continue;
                }

                await agent.SendUserAudioAsync(batch.WrittenSpan, ct).ConfigureAwait(false);
                batch.Clear();
                frames = 0;

                if (!_firstUplinkLogged)
                {
                    _firstUplinkLogged = true;
                    _logger.LogInformation("[latency] call {CallId} first user_audio_chunk sent {Ms} ms after answer", CallId, Ms(_createdAt));
                }
            }
        }
        catch (Exception ex) when (ex is OperationCanceledException || ct.IsCancellationRequested || !agent.IsOpen)
        {
            // normal teardown
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Uplink stopped for call {CallId}", CallId);
        }
    }

    public void OnConversationStarted(string? conversationId, string? inputFormat, string? outputFormat)
    {
        _logger.LogInformation("Call {CallId} ElevenLabs conversation {ConversationId} (in={In}, out={Out})",
            CallId, conversationId, inputFormat, outputFormat);
        if ((inputFormat is not null && inputFormat != "pcm_16000") || (outputFormat is not null && outputFormat != "pcm_16000"))
        {
            _logger.LogWarning("Agent audio formats are not pcm_16000 — fix the agent's Voice/Advanced settings; audio will be wrong");
        }
    }

    public void OnAgentAudio(byte[] pcm16k, long? eventId)
    {
        // Drop late chunks from a response the caller has already interrupted. Same rule as the ElevenLabs
        // JS client (play only if lastInterruptEventId <= audio_event.event_id).
        if (eventId is { } id && id < _lastInterruptionEventId)
        {
            return;
        }

        _playout.Append(pcm16k);
    }

    public void OnInterruption(long? eventId)
    {
        if (eventId is { } id)
        {
            _lastInterruptionEventId = id;
        }

        var droppedMs = _playout.Clear();
        _logger.LogInformation("[latency] call {CallId} interruption (event {EventId}) — dropped {DroppedMs} ms of queued agent audio",
            CallId, eventId, droppedMs);
    }

    public void OnUserTranscript(string text)
    {
        _lastUserTranscriptAt = Stopwatch.GetTimestamp();
        _logger.LogInformation("Call {CallId} user: {Text}", CallId, text);
    }

    public void OnAgentResponse(string text) => _logger.LogInformation("Call {CallId} agent: {Text}", CallId, text);

    private void OnUtteranceStarted()
    {
        if (_lastUserTranscriptAt != 0)
        {
            _logger.LogInformation("[latency] call {CallId} agent audio start, {Ms} ms after last user transcript", CallId, Ms(_lastUserTranscriptAt));
        }
        else
        {
            _logger.LogInformation("[latency] call {CallId} agent audio start (greeting), {Ms} ms after answer", CallId, Ms(_createdAt));
        }
    }

    private static string Ms(long since) => Stopwatch.GetElapsedTime(since).TotalMilliseconds.ToString("F0");

    // ---------- Teardown ----------

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1)
        {
            return;
        }

        _logger.LogInformation("Tearing down call {CallId}", CallId);
        _cts.Cancel();
        _socket.AudioMediaReceived -= OnAudioMediaReceived;
        _call.OnUpdated -= OnCallUpdated;
        if (IsMeeting)
        {
            _call.Participants.OnUpdated -= OnParticipantsUpdated;
        }

        if (_gateTimer is not null)
        {
            await _gateTimer.DisposeAsync().ConfigureAwait(false);
        }

        if (_speakerTimer is not null)
        {
            await _speakerTimer.DisposeAsync().ConfigureAwait(false);
        }

        if (_silencePump is not null)
        {
            await _silencePump.DisposeAsync().ConfigureAwait(false);
        }

        _reports?.Writer.TryComplete();

        _uplink.Writer.TryComplete();
        await _keepAlive.DisposeAsync().ConfigureAwait(false);

        // Never await the relay task unboundedly: it may be the one that triggered this dispose.
        await Task.WhenAny(_relayTask, Task.Delay(2000)).ConfigureAwait(false);
        await Task.WhenAny(_reportTask, Task.Delay(500)).ConfigureAwait(false);
        if (_agent is { } agent)
        {
            await agent.DisposeAsync().ConfigureAwait(false);
        }

        _agentCts?.Dispose();
        _playout.UtteranceStarted -= OnUtteranceStarted;
        _playout.Dispose();
        _cts.Dispose();
    }
}
