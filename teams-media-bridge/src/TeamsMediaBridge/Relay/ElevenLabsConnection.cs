using System.Buffers;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TeamsMediaBridge.Relay;

/// <summary>Callbacks for the server→client events the bridge cares about.</summary>
public interface IAgentEvents
{
    void OnConversationStarted(string? conversationId, string? inputFormat, string? outputFormat);
    void OnAgentAudio(byte[] pcm16k, long? eventId);
    void OnInterruption(long? eventId);
    void OnUserTranscript(string text);
    void OnAgentResponse(string text);
}

/// <summary>
/// One ElevenLabs Agents conversation over WebSocket.
/// Protocol: https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket
/// Bridge pattern (PCM 16 kHz both ways, ping→pong, interruption→flush):
/// https://elevenlabs.io/docs/eleven-agents/phone-numbers/microsoft-teams/graph-media-bot
/// The agent must be configured for pcm_16000 input AND output, or audio will be garbled.
/// </summary>
public sealed class ElevenLabsConnection : IAsyncDisposable
{
    private readonly ClientWebSocket _ws = new();
    // ClientWebSocket allows one outstanding send at a time; audio and pongs share this lock.
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private readonly ILogger _logger;

    public ElevenLabsConnection(ILogger logger)
    {
        _logger = logger;
        _ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
    }

    public bool IsOpen => _ws.State == WebSocketState.Open;

    /// <summary>Connect to the signed URL and send conversation_initiation_client_data as the very first message.</summary>
    public async Task ConnectAsync(Uri signedUrl, JsonObject initiationData, CancellationToken ct)
    {
        await _ws.ConnectAsync(signedUrl, ct).ConfigureAwait(false);
        await SendTextAsync(initiationData.ToJsonString(), ct).ConfigureAwait(false);
    }

    /// <summary>{"user_audio_chunk":"&lt;base64 PCM16 16 kHz mono&gt;"} — note: no "type" field on this message.</summary>
    public Task SendUserAudioAsync(ReadOnlySpan<byte> pcm, CancellationToken ct)
    {
        var json = "{\"user_audio_chunk\":\"" + Convert.ToBase64String(pcm) + "\"}";
        return SendTextAsync(json, ct);
    }

    /// <summary>Receive until the socket closes or <paramref name="ct"/> fires. Returns the close description.</summary>
    public async Task<string> RunReceiveLoopAsync(IAgentEvents sink, CancellationToken ct)
    {
        var buffer = ArrayPool<byte>.Shared.Rent(64 * 1024);
        var message = new ArrayBufferWriter<byte>(64 * 1024);
        try
        {
            while (_ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                message.Clear();
                WebSocketReceiveResult result;
                do
                {
                    result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        return $"closed by agent: {_ws.CloseStatus} {_ws.CloseStatusDescription}";
                    }

                    message.Write(buffer.AsSpan(0, result.Count));
                }
                while (!result.EndOfMessage);

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    await DispatchAsync(message.WrittenMemory, sink, ct).ConfigureAwait(false);
                }
            }

            return ct.IsCancellationRequested ? "cancelled" : $"socket state {_ws.State}";
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer);
        }
    }

    private async Task DispatchAsync(ReadOnlyMemory<byte> utf8, IAgentEvents sink, CancellationToken ct)
    {
        using var doc = JsonDocument.Parse(utf8);
        var root = doc.RootElement;
        var type = root.TryGetProperty("type", out var t) ? t.GetString() : null;

        switch (type)
        {
            case "audio":
                {
                    var ev = root.GetProperty("audio_event");
                    var b64 = ev.GetProperty("audio_base_64").GetString();
                    if (!string.IsNullOrEmpty(b64))
                    {
                        sink.OnAgentAudio(Convert.FromBase64String(b64), OptionalLong(ev, "event_id"));
                    }

                    break;
                }

            case "interruption":
                {
                    long? id = root.TryGetProperty("interruption_event", out var ie) ? OptionalLong(ie, "event_id") : null;
                    sink.OnInterruption(id);
                    break;
                }

            case "ping":
                {
                    var ev = root.GetProperty("ping_event");
                    var id = ev.GetProperty("event_id").GetInt64();
                    // Doc: reply {"type":"pong","event_id":N}. ping_ms (if present) is the server's suggested
                    // delay for simulated latency; we reply immediately.
                    await SendTextAsync($"{{\"type\":\"pong\",\"event_id\":{id}}}", ct).ConfigureAwait(false);
                    break;
                }

            case "user_transcript":
                sink.OnUserTranscript(root.GetProperty("user_transcription_event").GetProperty("user_transcript").GetString() ?? "");
                break;

            case "agent_response":
                sink.OnAgentResponse(root.GetProperty("agent_response_event").GetProperty("agent_response").GetString() ?? "");
                break;

            case "conversation_initiation_metadata":
                {
                    var ev = root.GetProperty("conversation_initiation_metadata_event");
                    sink.OnConversationStarted(
                        OptionalString(ev, "conversation_id"),
                        OptionalString(ev, "user_input_audio_format"),
                        OptionalString(ev, "agent_output_audio_format"));
                    break;
                }

            default:
                // vad_score, agent_response_correction, client_tool_call, etc. — not needed by a relay.
                _logger.LogDebug("Ignoring agent event {Type}", type);
                break;
        }
    }

    private async Task SendTextAsync(string json, CancellationToken ct)
    {
        var bytes = Encoding.UTF8.GetBytes(json);
        await _sendLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await _ws.SendAsync(bytes, WebSocketMessageType.Text, endOfMessage: true, ct).ConfigureAwait(false);
        }
        finally
        {
            _sendLock.Release();
        }
    }

    private static long? OptionalLong(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : null;

    private static string? OptionalString(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    /// <summary>
    /// Start a clean close (our close frame first). A pending <see cref="RunReceiveLoopAsync"/> then returns when
    /// the agent answers with its close frame. Errors are ignored: the caller disposes afterwards either way.
    /// </summary>
    public async Task CloseAsync(string reason)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            await _sendLock.WaitAsync(cts.Token).ConfigureAwait(false); // a close frame is a send: not alongside audio
            try
            {
                if (_ws.State == WebSocketState.Open)
                {
                    await _ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, reason, cts.Token).ConfigureAwait(false);
                }
            }
            finally
            {
                _sendLock.Release();
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "WebSocket close failed (ignored)");
        }
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (_ws.State is WebSocketState.Open or WebSocketState.CloseReceived)
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                await _ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "call ended", cts.Token).ConfigureAwait(false);
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "WebSocket close failed (ignored)");
        }
        finally
        {
            _ws.Dispose();
            _sendLock.Dispose();
        }
    }
}
