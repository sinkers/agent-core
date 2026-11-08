using System.ComponentModel.DataAnnotations;

namespace TeamsMediaBridge;

/// <summary>
/// Graph calling bot + media platform settings (config section "Bot").
/// Env var form: Bot__AppId, Bot__AppSecret, ... (double underscore).
/// </summary>
public sealed class BotOptions
{
    public const string Section = "Bot";

    /// <summary>Entra (AAD) application id of the single-tenant bot app.</summary>
    [Required] public string AppId { get; set; } = "";

    /// <summary>Client secret for <see cref="AppId"/>. Never put this in a committed file.</summary>
    [Required] public string AppSecret { get; set; } = "";

    /// <summary>Tenant the app is registered in; used as the token authority (single-tenant apps cannot use /common).</summary>
    [Required] public string TenantId { get; set; } = "";

    /// <summary>Public DNS name of the VM. Used for the Graph notification URL AND the media platform FQDN.</summary>
    [Required] public string ServiceFqdn { get; set; } = "";

    /// <summary>Thumbprint of a CA-signed cert for <see cref="ServiceFqdn"/> in LocalMachine\My.</summary>
    [Required] public string CertificateThumbprint { get; set; } = "";

    /// <summary>Public TCP port Teams media servers connect to (NSG + firewall must allow it).</summary>
    [Range(1, 65535)] public int InstancePublicPort { get; set; } = 8445;

    /// <summary>Local port the media platform listens on (same as public on a single VM with an ILPIP).</summary>
    [Range(1, 65535)] public int InstanceInternalPort { get; set; } = 8445;

    /// <summary>HTTPS port Kestrel listens on for Graph call notifications (/api/calling) and our own API.</summary>
    [Range(1, 65535)] public int CallSignalingPort { get; set; } = 443;

    /// <summary>Incoming calls beyond this are rejected as Busy.</summary>
    [Range(1, 100)] public int MaxConcurrentCalls { get; set; } = 4;
}

/// <summary>The agent's (Linux, Python) voice-session endpoint (config section "Agent").</summary>
public sealed class AgentOptions
{
    public const string Section = "Agent";

    /// <summary>e.g. https://agents.example.net/ada/voice/session</summary>
    [Required, Url] public string SessionUrl { get; set; } = "";

    /// <summary>Shared bearer token: sent to the agent, and required on POST /api/join.</summary>
    [Required, MinLength(16)] public string BridgeToken { get; set; } = "";

    [Range(1, 120)] public int RequestTimeoutSeconds { get; set; } = 10;
}

/// <summary>Audio relay tuning (config section "Relay").</summary>
public sealed class RelayOptions
{
    public const string Section = "Relay";

    /// <summary>Number of 20 ms Teams frames batched into one user_audio_chunk message (5 = 100 ms).</summary>
    [Range(1, 50)] public int FramesPerSend { get; set; } = 5;

    /// <summary>
    /// How many 20 ms frames we keep queued inside the AudioSocket. Absorbs timer jitter; it is also
    /// the only audio that can still play after a barge-in (3 = 60 ms).
    /// </summary>
    [Range(1, 25)] public int SocketQueueFrames { get; set; } = 3;

    /// <summary>Caller frames buffered while the ElevenLabs socket is connecting / slow (oldest dropped).</summary>
    [Range(10, 3000)] public int UplinkBufferFrames { get; set; } = 100;

    /// <summary>When the agent ends the conversation, let queued audio (goodbye) play for up to this long before hanging up.</summary>
    [Range(0, 60)] public int GoodbyeDrainSeconds { get; set; } = 10;

    /// <summary>Meetings: leave if no person has joined this long after the agent did (it never greets an empty room).</summary>
    [Range(1, 120)] public int MeetingWaitForPeopleMinutes { get; set; } = 15;

    /// <summary>Meetings: once everyone else has left, hang up after this long alone.</summary>
    [Range(10, 3600)] public int MeetingAloneLeaveSeconds { get; set; } = 120;

    /// <summary>Meetings: an unmixed frame counts as speech at or above this RMS (PCM16; 500 ≈ -36 dBFS).</summary>
    [Range(1, 32767)] public double SpeechRmsThreshold { get; set; } = 500;

    /// <summary>Meetings: keep counting a speaker as talking this long after their last loud frame.</summary>
    [Range(0, 2000)] public int SpeechHangoverMs { get; set; } = 200;

    /// <summary>Meetings: how often who-is-speaking is POSTed to the agent (only for windows with speech).</summary>
    [Range(100, 5000)] public int SpeakerWindowMs { get; set; } = 500;

    /// <summary>Meetings: after speech, this much silence sends one empty speakers report.</summary>
    [Range(200, 30000)] public int SpeakerSilenceMs { get; set; } = 1500;
}

/// <summary>File logging (config section "Logs").</summary>
public sealed class LogOptions
{
    public const string Section = "Logs";
    public string Directory { get; set; } = @"C:\teams-media-bridge\logs";
    public int RetainedDays { get; set; } = 14;
}
