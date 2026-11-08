using System.Collections.Concurrent;
using System.Net;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Extensions.Options;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Contracts;
using Microsoft.Graph.Models;
using Microsoft.Identity.Client;
using Microsoft.Skype.Bots.Media;
using TeamsMediaBridge.Relay;

namespace TeamsMediaBridge.Bot;

/// <summary>
/// Owns the Graph Communications client (stateful SDK + Real-time Media Platform) and the per-call relays.
/// Structure follows the EchoBot sample's BotService
/// (microsoft-graph-comms-samples/Samples/PublicSamples/EchoBot/src/EchoBot/Bot/BotService.cs).
/// </summary>
public sealed class BotService : IAsyncDisposable
{
    public const string CallbackRoute = "/api/calling";
    public const string NotificationRoute = "/api/calling/notification";

    private readonly BotOptions _bot;
    private readonly RelayOptions _relay;
    private readonly AgentSessionClient _agentClient;
    private readonly IGraphLogger _graphLogger;
    private readonly MediaPlatformLogger _mediaLogger;
    private readonly ILoggerFactory _loggerFactory;
    private readonly ILogger<BotService> _logger;
    private readonly ConcurrentDictionary<string, CallSession> _sessions = new();
    private readonly ConcurrentDictionary<Guid, string> _pendingJoins = new(); // scenarioId → requestedBy
    private readonly ConcurrentDictionary<Guid, OutboundCall> _pendingCalls = new(); // scenarioId → who we ring
    private readonly ConcurrentDictionary<string, byte> _rejected = new();     // calls we declined (still get "added")

    public BotService(
        IOptions<BotOptions> bot,
        IOptions<RelayOptions> relay,
        AgentSessionClient agentClient,
        IGraphLogger graphLogger,
        MediaPlatformLogger mediaLogger,
        ILoggerFactory loggerFactory)
    {
        _bot = bot.Value;
        _relay = relay.Value;
        _agentClient = agentClient;
        _graphLogger = graphLogger;
        _mediaLogger = mediaLogger;
        _loggerFactory = loggerFactory;
        _logger = loggerFactory.CreateLogger<BotService>();
    }

    public ICommunicationsClient Client { get; private set; } = null!;

    public int ActiveCalls => _sessions.Count;

    public IReadOnlyCollection<string> ActiveCallIds => _sessions.Keys.ToArray();

    /// <summary>Build the communications client and start the media platform. Windows x64 only.</summary>
    public void Initialize(X509Certificate2 certificate)
    {
        var builder = new CommunicationsClientBuilder("TeamsMediaBridge", _bot.AppId, _graphLogger);
        // Outbound Graph calls: app-only token via client credentials against OUR tenant (single-tenant app).
        // Inbound notification JWTs are validated by the SDK's DefaultAuthenticationProvider, which accepts both
        // the legacy Skype-signed tokens and the newer Entra-issued ones. The older
        // SetAuthenticationProvider(IRequestAuthenticationProvider) route used by the samples is [Obsolete] in 1.2.0.18736.
        var msal = ConfidentialClientApplicationBuilder.Create(_bot.AppId)
            .WithClientSecret(_bot.AppSecret)
            .WithAuthority($"https://login.microsoftonline.com/{_bot.TenantId}")
            .Build();
        builder.SetAuthentication(_bot.AppId, new MsalTokenProvider(msal));

        // Graph POSTs call state changes here. Must be publicly reachable HTTPS with a CA-signed cert.
        builder.SetNotificationUrl(new Uri($"https://{_bot.ServiceFqdn}:{_bot.CallSignalingPort}{NotificationRoute}"));
        builder.SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"));

        // Application-hosted media. On a single VM with an instance-level public IP:
        //  - ServiceFqdn must resolve to that public IP and match the certificate (media is TLS/SRTP-keyed off it);
        //  - InstancePublicPort is what Teams media servers connect to (TCP), InstanceInternalPort what we bind.
        // InstancePublicIPAddress = IPAddress.Any is what the EchoBot sample uses on a VM behind Azure NAT.
        // https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots
        builder.SetMediaPlatformSettings(new MediaPlatformSettings
        {
            ApplicationId = _bot.AppId,
            MediaPlatformLogger = _mediaLogger,
            MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
            {
                CertificateThumbprint = certificate.Thumbprint,
                InstanceInternalPort = _bot.InstanceInternalPort,
                InstancePublicPort = _bot.InstancePublicPort,
                InstancePublicIPAddress = IPAddress.Any,
                ServiceFqdn = _bot.ServiceFqdn,
            },
        });

        Client = builder.Build();
        Client.Calls().OnIncoming += OnIncomingCalls;
        Client.Calls().OnUpdated += OnCallsUpdated;
        _logger.LogInformation("Graph communications client ready; notifications → https://{Fqdn}:{Port}{Route}; media TCP {MediaPort}",
            _bot.ServiceFqdn, _bot.CallSignalingPort, NotificationRoute, _bot.InstancePublicPort);
    }

    /// <summary>
    /// Audio only, PCM 16 kHz, send + receive. Settings as in EchoBot's CreateLocalMediaSession; video is Inactive
    /// so no video socket work is done. Meetings (<paramref name="unmixed"/>) also receive up to four per-speaker
    /// streams (UnmixedAudioBuffers, for who-is-speaking). Unmixed mode alone delivers no mixed audio, so
    /// EnableLocalAudioMixingForUnmixed has the SDK mix them into Data as well (with audio healing on both);
    /// CallSession mixes the unmixed buffers itself if Data ever comes back empty. 1:1 calls are unchanged.
    /// </summary>
    private ILocalMediaSession CreateMediaSession(Guid mediaSessionId = default, bool unmixed = false) =>
        Client.CreateMediaSession(
            new AudioSocketSettings
            {
                StreamDirections = StreamDirection.Sendrecv,
                SupportedAudioFormat = AudioFormat.Pcm16K,
                ReceiveUnmixedMeetingAudio = unmixed,
                EnableLocalAudioMixingForUnmixed = unmixed,
            },
            new VideoSocketSettings { StreamDirections = StreamDirection.Inactive },
            mediaSessionId: mediaSessionId);

    private void OnIncomingCalls(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        foreach (var call in args.AddedResources)
        {
            var source = call.Resource.Source?.Identity;
            _logger.LogInformation("Incoming call {CallId} from {Name} ({AadId}, tenant {Tenant})",
                call.Id, source?.User?.DisplayName, source?.User?.Id, call.TenantId);

            if (_sessions.Count >= _bot.MaxConcurrentCalls)
            {
                _logger.LogWarning("Rejecting call {CallId}: {Count} calls already active", call.Id, _sessions.Count);
                _rejected[call.Id] = 0;
                call.RejectAsync(Microsoft.Graph.Models.RejectReason.Busy).Forget(_logger, $"Rejecting {call.Id}");
                continue;
            }

            // The sample reuses the call id as the media session id when it is a GUID.
            var media = Guid.TryParse(call.Id, out var id) ? CreateMediaSession(id) : CreateMediaSession();
            call.AnswerAsync(media).Forget(_logger, $"Answering call {call.Id}");
        }
    }

    private void OnCallsUpdated(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        foreach (var call in args.AddedResources)
        {
            if (_rejected.ContainsKey(call.Id))
            {
                continue;
            }

            string kind, aadId, name;
            string? callRef = null;
            if (_pendingCalls.TryRemove(call.ScenarioId, out var outbound))
            {
                kind = "teams-out";
                aadId = outbound.UserAadId;
                name = "";
                callRef = outbound.Ref;
            }
            else if (_pendingJoins.TryRemove(call.ScenarioId, out var requestedBy))
            {
                kind = "meeting";
                aadId = requestedBy;
                name = "";
            }
            else
            {
                var user = call.Resource.Source?.Identity?.User;
                kind = "p2p";
                aadId = user?.Id ?? "";
                name = user?.DisplayName ?? "";
            }

            try
            {
                var session = new CallSession(call, kind, aadId, name, _agentClient, _relay, _loggerFactory.CreateLogger<CallSession>(), callRef);
                if (_sessions.TryAdd(call.Id, session))
                {
                    session.Start();
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Could not start relay for call {CallId}; hanging up", call.Id);
                call.DeleteAsync().Forget(_logger, $"Deleting {call.Id}");
            }
        }

        foreach (var call in args.RemovedResources)
        {
            _rejected.TryRemove(call.Id, out _);
            if (_sessions.TryRemove(call.Id, out var session))
            {
                _ = Task.Run(async () => await session.DisposeAsync().ConfigureAwait(false));
            }

            _logger.LogInformation("Call {CallId} removed; {Count} active", call.Id, _sessions.Count);
        }
    }

    /// <summary>Join a Teams meeting by link with app-hosted media; the relay then runs with kind=meeting.</summary>
    public async Task<string> JoinMeetingAsync(string joinUrl, string requestedBy)
    {
        var parsed = MeetingJoinUrl.Parse(joinUrl);
        var scenarioId = Guid.NewGuid();
        var join = new JoinMeetingParameters(parsed.ChatInfo, parsed.MeetingInfo, CreateMediaSession(unmixed: true))
        {
            // A tenant-less short link can only be ours (single-tenant app).
            TenantId = parsed.TenantId ?? _bot.TenantId,
        };

        _pendingJoins[scenarioId] = requestedBy;
        try
        {
            var call = await Client.Calls().AddAsync(join, scenarioId).ConfigureAwait(false);
            _logger.LogInformation("Joining meeting as call {CallId} (requested by {RequestedBy})", call.Id, requestedBy);
            return call.Id;
        }
        catch
        {
            _pendingJoins.TryRemove(scenarioId, out _);
            throw;
        }
    }

    /// <summary>
    /// Ring one person in our tenant on Teams (1:1 VoIP call from the bot, app-hosted media). Needs the app's
    /// Calls.Initiate.All permission. <paramref name="callRef"/> is the agent's id for the call: the bridge passes it
    /// back on the session request so the agent can open with the reason and keep the transcript under it.
    /// </summary>
    public async Task<string> CallUserAsync(string userAadId, string callRef)
    {
        if (!Guid.TryParse(userAadId, out _))
        {
            throw new ArgumentException("userAadId must be an Entra object id");
        }

        if (_sessions.Count >= _bot.MaxConcurrentCalls)
        {
            throw new InvalidOperationException("busy");
        }

        var scenarioId = Guid.NewGuid();
        var resource = new Call
        {
            Targets = new List<InvitationParticipantInfo>
            {
                new() { Identity = new IdentitySet { User = new Identity { Id = userAadId } } },
            },
            RequestedModalities = new List<Modality?> { Modality.Audio },
            TenantId = _bot.TenantId,
        };

        _pendingCalls[scenarioId] = new OutboundCall(userAadId, callRef);
        try
        {
            var call = await Client.Calls().AddAsync(resource, CreateMediaSession(), null, scenarioId).ConfigureAwait(false);
            _logger.LogInformation("Calling {UserAadId} on Teams as call {CallId} (ref {Ref})", userAadId, call.Id, callRef);
            return call.Id;
        }
        catch
        {
            _pendingCalls.TryRemove(scenarioId, out _);
            throw;
        }
    }

    /// <summary>
    /// Swap the ElevenLabs conversation on a live call for a fresh one from the agent, without hanging up.
    /// <paramref name="callId"/> is the Graph call id (what the bridge sends as call_id) or the agent's ref for an
    /// outbound call.
    /// </summary>
    public ReconnectResult Reconnect(string? callId) =>
        ReconnectRouting.Find(_sessions.Values, callId, s => s.CallId, s => s.CallRef)?.RequestReconnect()
            ?? ReconnectResult.NotFound;

    public async ValueTask DisposeAsync()
    {
        if (Client is null)
        {
            return;
        }

        _logger.LogWarning("Shutting down: terminating {Count} calls", _sessions.Count);
        try
        {
            // Hangs up every call and shuts the media platform down (EchoBot BotService.Shutdown).
            await Client.TerminateAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "TerminateAsync failed");
        }

        foreach (var session in _sessions.Values)
        {
            await session.DisposeAsync().ConfigureAwait(false);
        }

        _sessions.Clear();
        Client.Dispose();
    }

    /// <summary>
    /// Find the TLS/media certificate in LocalMachine\My. Prefers the configured thumbprint; if it is gone
    /// (e.g. win-acme renewed and the config was not updated yet) falls back to the newest valid cert whose
    /// DNS name matches ServiceFqdn, and says so loudly.
    /// </summary>
    public static X509Certificate2 FindCertificate(BotOptions bot, ILogger logger)
    {
        using var store = new X509Store(StoreName.My, StoreLocation.LocalMachine);
        store.Open(OpenFlags.ReadOnly);
        var thumb = bot.CertificateThumbprint.Replace(" ", string.Empty);
        var byThumb = store.Certificates.Find(X509FindType.FindByThumbprint, thumb, validOnly: false);
        if (byThumb.Count == 1)
        {
            return byThumb[0];
        }

        var fallback = store.Certificates
            .Where(c => c.HasPrivateKey && c.NotAfter > DateTime.Now &&
                        string.Equals(c.GetNameInfo(X509NameType.DnsName, false), bot.ServiceFqdn, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(c => c.NotAfter)
            .FirstOrDefault()
            ?? throw new InvalidOperationException(
                $"No certificate with thumbprint {thumb} (or for {bot.ServiceFqdn}) in LocalMachine\\My");
        logger.LogWarning("Certificate {Thumb} not found; using {Fallback} for {Fqdn}. Update Bot:CertificateThumbprint.",
            thumb, fallback.Thumbprint, bot.ServiceFqdn);
        return fallback;
    }
}

internal static class TaskExtensions
{
    /// <summary>Fire-and-forget with the exception logged instead of lost (the samples' ForgetAndLogExceptionAsync).</summary>
    public static void Forget(this Task task, ILogger logger, string description) =>
        task.ContinueWith(
            t => logger.LogError(t.Exception, "{Description} failed", description),
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted,
            TaskScheduler.Default);
}

/// <summary>An outbound Teams call waiting for its call resource.</summary>
public sealed record OutboundCall(string UserAadId, string Ref);
