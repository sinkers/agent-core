using Microsoft.Graph.Models;

namespace TeamsMediaBridge.Relay;

/// <summary>What a meeting call should do next.</summary>
public enum GateAction
{
    None,
    StartAgent,
    Leave,
}

/// <summary>
/// Meetings only: The agent joins at the start time but stays silent (no agent conversation, no greeting) until a
/// person is in the meeting. If nobody turns up within <c>waitForPeople</c> he leaves; once people have been and
/// gone, he leaves after <c>aloneGrace</c> on his own. Pure logic, driven by participant updates and a timer.
/// </summary>
public sealed class MeetingGate
{
    private readonly TimeSpan _waitForPeople;
    private readonly TimeSpan _aloneGrace;
    private readonly DateTimeOffset _joinedAt;
    private DateTimeOffset? _aloneSince;

    public MeetingGate(DateTimeOffset joinedAt, TimeSpan waitForPeople, TimeSpan aloneGrace)
    {
        _joinedAt = joinedAt;
        _waitForPeople = waitForPeople;
        _aloneGrace = aloneGrace;
    }

    public bool AgentStarted { get; private set; }

    public GateAction Update(int people, DateTimeOffset now)
    {
        if (!AgentStarted)
        {
            if (people > 0)
            {
                AgentStarted = true;
                return GateAction.StartAgent;
            }

            return now - _joinedAt >= _waitForPeople ? GateAction.Leave : GateAction.None;
        }

        if (people > 0)
        {
            _aloneSince = null;
            return GateAction.None;
        }

        _aloneSince ??= now;
        return now - _aloneSince.Value >= _aloneGrace ? GateAction.Leave : GateAction.None;
    }

    /// <summary>
    /// People in the meeting: participants that are not applications (the agent itself, other bots and note-takers)
    /// and not waiting in the lobby.
    /// </summary>
    public static int CountPeople(IEnumerable<Participant?> participants) =>
        participants.Count(p =>
        {
            var id = p?.Info?.Identity;
            if (id is null || id.Application is not null)
            {
                return false;
            }

            if (id is CommunicationsIdentitySet c && c.ApplicationInstance is not null)
            {
                return false;
            }

            return p!.IsInLobby != true;
        });
}
