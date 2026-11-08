using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Options;

namespace TeamsMediaBridge.Relay;

/// <summary>What the agent hands back for one call: where to connect and what to send first.</summary>
public sealed record AgentSession(Uri SignedUrl, JsonObject InitiationData);

/// <summary>
/// Asks the agent (Linux) for an ElevenLabs conversation for this call. The agent owns the ElevenLabs API key,
/// agent id, prompt and dynamic variables; the bridge only ever sees a short-lived signed URL.
/// GET {SessionUrl}?call_id=..&amp;caller_aad_id=..&amp;caller_name=..&amp;kind=p2p|meeting
/// Authorization: Bearer {BridgeToken}
/// → {"signed_url":"wss://...","conversation_initiation_client_data":{...}}
/// </summary>
public sealed class AgentSessionClient
{
    public const string HttpClientName = "agent";

    private readonly IHttpClientFactory _httpFactory;
    private readonly AgentOptions _options;
    private readonly ILogger<AgentSessionClient> _logger;

    public AgentSessionClient(IHttpClientFactory httpFactory, IOptions<AgentOptions> options, ILogger<AgentSessionClient> logger)
    {
        _httpFactory = httpFactory;
        _options = options.Value;
        _logger = logger;
    }

    public async Task<AgentSession> GetSessionAsync(string callId, string? callerAadId, string? callerName, string kind, CancellationToken ct, string? callRef = null, bool reconnect = false)
    {
        var query = System.Web.HttpUtility.ParseQueryString(string.Empty);
        query["call_id"] = callId;
        query["caller_aad_id"] = callerAadId ?? string.Empty;
        query["caller_name"] = callerName ?? string.Empty;
        query["kind"] = kind;
        if (!string.IsNullOrEmpty(callRef))
        {
            query["ref"] = callRef; // outbound call: the agent's own id for it (opener, reason, transcript)
        }

        if (reconnect)
        {
            query["reconnect"] = "1"; // a fresh conversation for a call already in progress (POST /api/voice/reconnect)
        }

        var uri = new UriBuilder(_options.SessionUrl) { Query = query.ToString() }.Uri;

        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _options.BridgeToken);

        var client = _httpFactory.CreateClient(HttpClientName);
        client.Timeout = TimeSpan.FromSeconds(_options.RequestTimeoutSeconds);

        var started = System.Diagnostics.Stopwatch.GetTimestamp();
        using var response = await client.SendAsync(request, ct).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            // Body may explain the refusal (unknown caller etc.); never contains our token.
            throw new HttpRequestException($"agent session endpoint returned {(int)response.StatusCode}: {Truncate(body, 300)}");
        }

        var json = JsonNode.Parse(body) as JsonObject
            ?? throw new InvalidOperationException("the agent session response is not a JSON object");
        var signedUrl = json["signed_url"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(signedUrl) || !Uri.TryCreate(signedUrl, UriKind.Absolute, out var wss) || wss.Scheme != "wss")
        {
            throw new InvalidOperationException("the agent session response has no valid wss:// signed_url");
        }

        // Detach the node from its parent so we can own/mutate it.
        var init = json["conversation_initiation_client_data"]?.DeepClone() as JsonObject ?? new JsonObject();
        // ElevenLabs requires the discriminator on this message; add it if the agent left it out.
        init["type"] ??= "conversation_initiation_client_data";

        _logger.LogInformation(
            "the agent session for call {CallId} ready in {Ms} ms (host {Host})",
            callId, System.Diagnostics.Stopwatch.GetElapsedTime(started).TotalMilliseconds.ToString("F0"), wss.Host);
        return new AgentSession(wss, init);
    }

    /// <summary>
    /// Tell the agent how a call's agent conversation ended (POST to the same session URL), so failures such as
    /// ElevenLabs running out of credits reach a person instead of the call just dropping. Best effort.
    /// </summary>
    public async Task ReportEndAsync(string callId, string kind, string reason, double secondsLive)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, _options.SessionUrl)
            {
                Content = JsonContent.Create(new { call_id = callId, kind, reason = Truncate(reason, 500), seconds = secondsLive }),
            };
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _options.BridgeToken);
            var client = _httpFactory.CreateClient(HttpClientName);
            client.Timeout = TimeSpan.FromSeconds(_options.RequestTimeoutSeconds);
            using var response = await client.SendAsync(request).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning("Could not report end of call {CallId}: {Error}", callId, ex.Message);
        }
    }

    /// <summary>
    /// POST one speakers window ({"type":"speakers",...}) to the session URL. The caller runs this off the media
    /// thread; failures are returned (false) rather than thrown so the caller can rate-limit its own logging.
    /// </summary>
    public async Task<bool> PostSpeakersAsync(SpeakersReport report, CancellationToken ct)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, _options.SessionUrl) { Content = JsonContent.Create(report) };
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _options.BridgeToken);
            var client = _httpFactory.CreateClient(HttpClientName);
            client.Timeout = TimeSpan.FromSeconds(Math.Min(_options.RequestTimeoutSeconds, 5));
            using var response = await client.SendAsync(request, ct).ConfigureAwait(false);
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or OperationCanceledException)
        {
            return false;
        }
    }

    private static string Truncate(string s, int max) => s.Length <= max ? s : s[..max] + "…";
}
