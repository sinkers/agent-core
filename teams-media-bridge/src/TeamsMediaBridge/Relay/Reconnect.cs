namespace TeamsMediaBridge.Relay;

/// <summary>Outcome of POST /api/voice/reconnect for one call.</summary>
public enum ReconnectResult
{
    /// <summary>The call is live: its ElevenLabs conversation is being swapped for a fresh one (202).</summary>
    Accepted,

    /// <summary>No active call has that id (404).</summary>
    NotFound,

    /// <summary>The call is live but its agent hasn't started yet (a meeting waiting for people), so there is nothing to swap (409).</summary>
    NotStarted,
}

/// <summary>Which live call a reconnect request means: The agent may know it by the Graph call id or by its own ref.</summary>
public static class ReconnectRouting
{
    public static bool Matches(string? requested, string callId, string? callRef) =>
        !string.IsNullOrWhiteSpace(requested)
        && (string.Equals(requested, callId, StringComparison.OrdinalIgnoreCase)
            || (!string.IsNullOrEmpty(callRef) && string.Equals(requested, callRef, StringComparison.Ordinal)));

    /// <summary>Pick the session for <paramref name="requested"/> from the live ones.</summary>
    public static T? Find<T>(IEnumerable<T> sessions, string? requested, Func<T, string> callId, Func<T, string?> callRef)
        where T : class =>
        sessions.FirstOrDefault(s => Matches(requested, callId(s), callRef(s)));
}
