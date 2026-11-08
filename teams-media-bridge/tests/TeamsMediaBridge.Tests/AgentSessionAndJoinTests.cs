using System.Net;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Graph.Models;
using TeamsMediaBridge.Bot;
using TeamsMediaBridge.Http;
using TeamsMediaBridge.Relay;
using Xunit;
using MsOptions = Microsoft.Extensions.Options.Options;

namespace TeamsMediaBridge.Tests;

public class AgentSessionClientTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        private readonly HttpStatusCode _status;
        private readonly string _body;
        public HttpRequestMessage? Last;

        public StubHandler(HttpStatusCode status, string body) => (_status, _body) = (status, body);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Last = request;
            return Task.FromResult(new HttpResponseMessage(_status) { Content = new StringContent(_body, Encoding.UTF8, "application/json") });
        }
    }

    private sealed class Factory : IHttpClientFactory
    {
        private readonly HttpMessageHandler _h;
        public Factory(HttpMessageHandler h) => _h = h;
        public HttpClient CreateClient(string name) => new(_h, disposeHandler: false);
    }

    private static (AgentSessionClient, StubHandler) Create(HttpStatusCode status, string body)
    {
        var handler = new StubHandler(status, body);
        var options = MsOptions.Create(new AgentOptions
        {
            SessionUrl = "https://agents.example.net/ada/voice/session",
            BridgeToken = "test-token-0123456789",
        });
        return (new AgentSessionClient(new Factory(handler), options, NullLogger<AgentSessionClient>.Instance), handler);
    }

    [Fact]
    public async Task Sends_query_and_bearer_and_adds_missing_type()
    {
        var (client, handler) = Create(HttpStatusCode.OK,
            """{"signed_url":"wss://api.elevenlabs.io/v1/convai/conversation?agent_id=x&token=y","conversation_initiation_client_data":{"dynamic_variables":{"caller":"Alex"}}}""");

        var session = await client.GetSessionAsync("call-1", "aad-2", "Alex Owner", "p2p", CancellationToken.None);

        var q = System.Web.HttpUtility.ParseQueryString(handler.Last!.RequestUri!.Query);
        Assert.Equal("call-1", q["call_id"]);
        Assert.Equal("aad-2", q["caller_aad_id"]);
        Assert.Equal("Alex Owner", q["caller_name"]);
        Assert.Equal("p2p", q["kind"]);
        Assert.Equal("Bearer", handler.Last.Headers.Authorization!.Scheme);
        Assert.Equal("test-token-0123456789", handler.Last.Headers.Authorization.Parameter);

        Assert.Equal("wss", session.SignedUrl.Scheme);
        Assert.Equal("conversation_initiation_client_data", session.InitiationData["type"]!.GetValue<string>());
        Assert.Equal("Alex", session.InitiationData["dynamic_variables"]!["caller"]!.GetValue<string>());
    }

    [Fact]
    public async Task Outbound_calls_pass_their_ref_and_others_do_not()
    {
        const string ok = """{"signed_url":"wss://x.example/ws"}""";
        var (client, handler) = Create(HttpStatusCode.OK, ok);
        await client.GetSessionAsync("graph-call", "aad-9", null, "teams-out", CancellationToken.None, "tout-abc123");
        var q = System.Web.HttpUtility.ParseQueryString(handler.Last!.RequestUri!.Query);
        Assert.Equal("teams-out", q["kind"]);
        Assert.Equal("tout-abc123", q["ref"]);

        await client.GetSessionAsync("graph-call-2", "aad-9", null, "p2p", CancellationToken.None);
        q = System.Web.HttpUtility.ParseQueryString(handler.Last!.RequestUri!.Query);
        Assert.Null(q["ref"]);
    }

    [Fact]
    public async Task Keeps_existing_type_and_tolerates_missing_init_data()
    {
        var (client, _) = Create(HttpStatusCode.OK, """{"signed_url":"wss://x.example/ws"}""");
        var session = await client.GetSessionAsync("c", null, null, "meeting", CancellationToken.None);
        Assert.Equal("conversation_initiation_client_data", session.InitiationData["type"]!.GetValue<string>());
    }

    [Theory]
    [InlineData(HttpStatusCode.OK, """{"signed_url":"https://not-a-websocket"}""")]
    [InlineData(HttpStatusCode.OK, """{"nope":1}""")]
    [InlineData(HttpStatusCode.Forbidden, """{"error":"unknown caller"}""")]
    public async Task Rejects_bad_responses(HttpStatusCode status, string body)
    {
        var (client, _) = Create(status, body);
        await Assert.ThrowsAnyAsync<Exception>(() => client.GetSessionAsync("c", null, null, "p2p", CancellationToken.None));
    }
}

public class MeetingJoinUrlTests
{
    [Fact]
    public void Parses_classic_meetup_join_link()
    {
        var url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%22Tid%22%3a%22tenant-1%22%2c%22Oid%22%3a%22organizer-1%22%7d";
        var parsed = MeetingJoinUrl.Parse(url);

        Assert.Equal("19:meeting_abc@thread.v2", parsed.ChatInfo!.ThreadId);
        Assert.Equal("0", parsed.ChatInfo.MessageId);
        Assert.Equal("tenant-1", parsed.TenantId);
        var organizer = Assert.IsType<OrganizerMeetingInfo>(parsed.MeetingInfo).Organizer!.User!;
        Assert.Equal("organizer-1", organizer.Id);
    }

    [Fact]
    public void Parses_short_meet_link_as_join_meeting_id()
    {
        var parsed = MeetingJoinUrl.Parse("https://teams.microsoft.com/meet/123456789012?p=AbCdEf");
        var info = Assert.IsType<JoinMeetingIdMeetingInfo>(parsed.MeetingInfo);
        Assert.Equal("123456789012", info.JoinMeetingId);
        Assert.Equal("AbCdEf", info.Passcode);
        Assert.Null(parsed.ChatInfo);
    }

    [Theory]
    [InlineData("")]
    [InlineData("https://example.com/whatever")]
    public void Rejects_other_urls(string url) => Assert.Throws<ArgumentException>(() => MeetingJoinUrl.Parse(url));
}

public class BearerTests
{
    private static HttpRequest With(string? header)
    {
        var ctx = new DefaultHttpContext();
        if (header is not null)
        {
            ctx.Request.Headers.Authorization = header;
        }

        return ctx.Request;
    }

    [Theory]
    [InlineData("Bearer s3cret-token-value", true)]
    [InlineData("bearer s3cret-token-value", true)]
    [InlineData("Bearer wrong", false)]
    [InlineData("Basic s3cret-token-value", false)]
    [InlineData(null, false)]
    public void Checks_bearer_token(string? header, bool expected) =>
        Assert.Equal(expected, With(header).HasBearer("s3cret-token-value"));

    [Fact]
    public void Empty_expected_token_never_matches() => Assert.False(With("Bearer ").HasBearer(""));
}
