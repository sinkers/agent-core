using Microsoft.Graph.Models;
using TeamsMediaBridge.Relay;
using Xunit;

namespace TeamsMediaBridge.Tests;

public class MeetingGateTests
{
    private static readonly DateTimeOffset T0 = new(2026, 10, 5, 21, 29, 0, TimeSpan.Zero);

    private static MeetingGate Gate() => new(T0, TimeSpan.FromMinutes(15), TimeSpan.FromMinutes(2));

    [Fact]
    public void Stays_silent_in_an_empty_meeting_then_starts_when_someone_joins()
    {
        var gate = Gate();
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddSeconds(5)));
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddMinutes(3)));
        Assert.False(gate.AgentStarted);
        Assert.Equal(GateAction.StartAgent, gate.Update(1, T0.AddMinutes(4)));
        Assert.Equal(GateAction.None, gate.Update(3, T0.AddMinutes(5)));  // starts once
    }

    [Fact]
    public void Leaves_if_nobody_turns_up()
    {
        var gate = Gate();
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddMinutes(14)));
        Assert.Equal(GateAction.Leave, gate.Update(0, T0.AddMinutes(15)));
    }

    [Fact]
    public void Leaves_two_minutes_after_the_last_person_unless_someone_comes_back()
    {
        var gate = Gate();
        gate.Update(2, T0.AddMinutes(1));
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddMinutes(10)));
        Assert.Equal(GateAction.None, gate.Update(1, T0.AddMinutes(11)));      // came back: timer resets
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddMinutes(12)));
        Assert.Equal(GateAction.None, gate.Update(0, T0.AddMinutes(13)));
        Assert.Equal(GateAction.Leave, gate.Update(0, T0.AddMinutes(14)));
    }

    private static Participant Person(string id, bool lobby = false) =>
        new() { Info = new ParticipantInfo { Identity = new IdentitySet { User = new Identity { Id = id } } }, IsInLobby = lobby };

    [Fact]
    public void Counts_people_but_not_bots_or_the_lobby()
    {
        var bot = new Participant { Info = new ParticipantInfo { Identity = new IdentitySet { Application = new Identity { Id = "agent-app" } } } };
        var notetaker = new Participant { Info = new ParticipantInfo { Identity = new CommunicationsIdentitySet { ApplicationInstance = new Identity { Id = "x" } } } };
        Assert.Equal(0, MeetingGate.CountPeople(new[] { bot, notetaker }));
        Assert.Equal(0, MeetingGate.CountPeople(new[] { bot, Person("erin", lobby: true) }));
        Assert.Equal(2, MeetingGate.CountPeople(new Participant?[] { bot, Person("erin"), Person("alex"), null }));
    }
}
