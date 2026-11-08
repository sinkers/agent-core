using System.Buffers.Binary;
using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Graph.Models;
using TeamsMediaBridge.Relay;
using Xunit;
using MsOptions = Microsoft.Extensions.Options.Options;

namespace TeamsMediaBridge.Tests;

public class Pcm16Tests
{
    private static byte[] Frame(params short[] samples)
    {
        var b = new byte[samples.Length * 2];
        for (var i = 0; i < samples.Length; i++)
        {
            BinaryPrimitives.WriteInt16LittleEndian(b.AsSpan(i * 2), samples[i]);
        }

        return b;
    }

    private static short At(byte[] b, int i) => BinaryPrimitives.ReadInt16LittleEndian(b.AsSpan(i * 2));

    [Fact]
    public void Rms_of_silence_is_zero_and_of_a_square_wave_is_its_amplitude()
    {
        Assert.Equal(0, Pcm16.Rms(new byte[640]));
        Assert.Equal(0, Pcm16.Rms(ReadOnlySpan<byte>.Empty));
        Assert.Equal(1000, Pcm16.Rms(Frame(1000, -1000, 1000, -1000)), 3);
    }

    [Fact]
    public void Mix_sums_streams_and_clips_to_int16()
    {
        var mixed = Pcm16.Mix(new[] { Frame(1000, 30000, -30000, 5), Frame(-400, 10000, -10000) }, 8);
        Assert.Equal(8, mixed.Length);
        Assert.Equal(600, At(mixed, 0));
        Assert.Equal(short.MaxValue, At(mixed, 1));
        Assert.Equal(short.MinValue, At(mixed, 2));
        Assert.Equal(5, At(mixed, 3)); // the shorter stream is silent for the tail
    }

    [Fact]
    public void Mix_of_one_stream_pads_or_cuts_to_the_frame()
    {
        Assert.Equal(new byte[] { 1, 0, 2, 0, 0, 0 }, Pcm16.Mix(new[] { Frame(1, 2) }, 6));
        Assert.Equal(new byte[] { 1, 0 }, Pcm16.Mix(new[] { Frame(1, 2) }, 2));
    }
}

public class SpeakerRosterTests
{
    private static Participant WithAudio(IdentitySet identity, params string[] sourceIds) => new()
    {
        Info = new ParticipantInfo { Identity = identity },
        MediaStreams = sourceIds.Select(id => new MediaStream { MediaType = Modality.Audio, SourceId = id }).ToList(),
    };

    [Fact]
    public void Maps_audio_source_ids_to_people_and_ignores_video()
    {
        var alex = WithAudio(new IdentitySet { User = new Identity { Id = "aad-alex", DisplayName = "Alex Owner" } }, "101");
        alex.MediaStreams!.Add(new MediaStream { MediaType = Modality.Video, SourceId = "900" });
        var guest = WithAudio(new CommunicationsIdentitySet { Guest = new Identity { Id = "g1", DisplayName = "Pat (Guest)" } }, "102");
        var phone = WithAudio(new CommunicationsIdentitySet { Phone = new Identity { Id = "+61", DisplayName = "+61 400 000 000" } }, "103");
        var broken = WithAudio(new IdentitySet { User = new Identity { Id = "x" } }, "not-a-number");

        var map = SpeakerRoster.Build(new Participant?[] { alex, guest, phone, broken, null, new Participant() });

        Assert.Equal(new SpeakerIdentity("aad-alex", "Alex Owner"), map[101]);
        Assert.Equal(new SpeakerIdentity("", "Pat (Guest)"), map[102]);   // only Entra users get an aad_id
        Assert.Equal(new SpeakerIdentity("", "+61 400 000 000"), map[103]);
        Assert.False(map.ContainsKey(900));
        Assert.Equal(3, map.Count);
    }

    [Fact]
    public void Update_replaces_the_map_so_people_who_left_drop_out()
    {
        var roster = new SpeakerRoster();
        var a = WithAudio(new IdentitySet { User = new Identity { Id = "a", DisplayName = "A" } }, "1");
        var b = WithAudio(new IdentitySet { User = new Identity { Id = "b", DisplayName = "B" } }, "2");
        roster.Update(new[] { a, b });
        Assert.True(roster.TryGet(2, out var who));
        Assert.Equal("B", who.Name);

        roster.Update(new[] { a });
        Assert.False(roster.TryGet(2, out who));
        Assert.Equal(new SpeakerIdentity("", ""), who);
        Assert.Equal(1, roster.Count);
    }
}

public class SpeakerTrackerTests
{
    private const double Threshold = 500;

    private static SpeakerTracker Tracker() => new(Threshold, hangoverMs: 200, windowMs: 500, silenceMs: 1500);

    private static SpeakerRoster Roster()
    {
        var roster = new SpeakerRoster();
        roster.Update(new[]
        {
            new Participant
            {
                Info = new ParticipantInfo { Identity = new IdentitySet { User = new Identity { Id = "aad-erin", DisplayName = "Erin" } } },
                MediaStreams = new List<MediaStream> { new() { MediaType = Modality.Audio, SourceId = "7" } },
            },
        });
        return roster;
    }

    /// <summary>Feed 20 ms frames for msi from t to t+ms at a fixed level.</summary>
    private static long Talk(SpeakerTracker t, uint msi, double rms, long from, int ms)
    {
        for (var at = from; at < from + ms; at += 20)
        {
            t.AddLevel(msi, rms, 20, at);
        }

        return from + ms;
    }

    [Fact]
    public void Quiet_frames_are_not_speech_until_someone_is_loud()
    {
        var t = Tracker();
        Assert.False(t.AddLevel(7, Threshold - 1, 20, 0));
        Assert.True(t.AddLevel(7, Threshold, 20, 20));
        Assert.True(t.AddLevel(7, 10, 20, 220));  // within the 200 ms hangover
        Assert.False(t.AddLevel(7, 10, 20, 240)); // hangover over
        Assert.False(t.AddLevel(8, 10, 20, 240)); // hangover is per speaker
    }

    [Fact]
    public void Window_reports_speech_ms_per_speaker_with_names_loudest_first()
    {
        var t = Tracker();
        Talk(t, 7, 2000, 0, 340);
        Talk(t, 9, 900, 0, 100);
        Talk(t, 9, 50, 100, 400); // quiet: only the 200 ms hangover counts

        var report = t.Flush("call-1", 500, 1_760_000_000_500, Roster());

        Assert.NotNull(report);
        Assert.Equal("speakers", report!.Type);
        Assert.Equal("call-1", report.CallId);
        Assert.Equal(1_760_000_000_500, report.AtMs);
        Assert.Equal(500, report.WindowMs);
        Assert.Equal(2, report.Speakers.Count);
        Assert.Equal(new SpeakerEntry(7, "aad-erin", "Erin", 340), report.Speakers[0]);
        Assert.Equal(new SpeakerEntry(9, "", "", 300), report.Speakers[1]); // not on the roster yet
        Assert.True(SpeakerTracker.HasUnknown(report));
    }

    [Fact]
    public void Silent_windows_report_nothing_then_one_empty_report_after_1_5_s()
    {
        var t = Tracker();
        var roster = Roster();
        Assert.Null(t.Flush("c", 500, 0, roster)); // nobody has spoken: no silence event either

        var end = Talk(t, 7, 2000, 500, 200);       // last loud frame at 680
        Assert.Single(t.Flush("c", 1000, 0, roster)!.Speakers);
        Assert.Null(t.Flush("c", 1500, 0, roster));
        Assert.Null(t.Flush("c", 2000, 0, roster));  // 1320 ms of silence
        var silence = t.Flush("c", 2500, 0, roster); // 1820 ms
        Assert.NotNull(silence);
        Assert.Empty(silence!.Speakers);
        Assert.Null(t.Flush("c", 3000, 0, roster));  // once per silence
        Assert.Null(t.Flush("c", 9000, 0, roster));

        Talk(t, 7, 2000, 9000, 100);
        Assert.NotNull(t.Flush("c", 9500, 0, roster));
        Assert.Empty(t.Flush("c", 11000, 0, roster)!.Speakers); // and again after the next speech
        Assert.True(end > 0);
    }

    [Fact]
    public void Active_speaker_fallback_counts_frames_and_caps_at_the_window()
    {
        var t = Tracker();
        for (var at = 0; at < 600; at += 20)
        {
            t.AddActive(new uint[] { 7, 8 }, 20, at);
        }

        var report = t.Flush("c", 600, 0, Roster())!;
        Assert.All(report.Speakers, s => Assert.Equal(500, s.SpeechMs));
        Assert.Equal(new uint[] { 7, 8 }, report.Speakers.Select(s => s.Msi).OrderBy(m => m));
    }

    [Fact]
    public void Report_serialises_to_the_sessions_contract()
    {
        var report = new SpeakersReport("speakers", "call-1", 123, 500, new[] { new SpeakerEntry(7, "aad", "Erin", 340) });
        Assert.Equal(
            """{"type":"speakers","call_id":"call-1","at_ms":123,"window_ms":500,"speakers":[{"msi":7,"aad_id":"aad","name":"Erin","speech_ms":340}]}""",
            JsonSerializer.Serialize(report));
        Assert.Equal(
            """{"type":"speakers","call_id":"c","at_ms":1,"window_ms":500,"speakers":[]}""",
            JsonSerializer.Serialize(new SpeakersReport("speakers", "c", 1, 500, Array.Empty<SpeakerEntry>())));
    }
}

public class ReconnectTests
{
    private sealed record Live(string CallId, string? CallRef);

    [Fact]
    public void Finds_the_call_by_graph_id_or_by_sessions_ref()
    {
        var calls = new[] { new Live("2f1a0000-1111-2222-3333-444455556666", null), new Live("9b2e0000-aaaa-bbbb-cccc-ddddeeeeffff", "tout-abc123") };
        Live? Find(string? id) => ReconnectRouting.Find(calls, id, c => c.CallId, c => c.CallRef);

        Assert.Same(calls[0], Find("2f1a0000-1111-2222-3333-444455556666"));
        Assert.Same(calls[0], Find("2F1A0000-1111-2222-3333-444455556666")); // Graph ids are GUIDs
        Assert.Same(calls[1], Find("tout-abc123"));
        Assert.Null(Find("TOUT-ABC123"));
        Assert.Null(Find("nope"));
        Assert.Null(Find(""));
        Assert.Null(Find(null));
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        public HttpRequestMessage? Last;
        public string? Body;
        public HttpStatusCode Status = HttpStatusCode.OK;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Last = request;
            Body = request.Content is null ? null : await request.Content.ReadAsStringAsync(ct);
            return new HttpResponseMessage(Status) { Content = new StringContent("""{"signed_url":"wss://x.example/ws"}""", Encoding.UTF8, "application/json") };
        }
    }

    private sealed class Factory(HttpMessageHandler h) : IHttpClientFactory
    {
        public HttpClient CreateClient(string name) => new(h, disposeHandler: false);
    }

    private static (AgentSessionClient, StubHandler) Client()
    {
        var handler = new StubHandler();
        var options = MsOptions.Create(new AgentOptions { SessionUrl = "https://agents.example/ada/voice/session", BridgeToken = "tok-0123456789" });
        return (new AgentSessionClient(new Factory(handler), options, NullLogger<AgentSessionClient>.Instance), handler);
    }

    [Fact]
    public async Task Reconnect_session_request_adds_reconnect_1_and_first_request_does_not()
    {
        var (client, handler) = Client();
        await client.GetSessionAsync("call-1", "aad", "A", "meeting", CancellationToken.None);
        Assert.Null(System.Web.HttpUtility.ParseQueryString(handler.Last!.RequestUri!.Query)["reconnect"]);

        await client.GetSessionAsync("call-1", "aad", "A", "meeting", CancellationToken.None, reconnect: true);
        var q = System.Web.HttpUtility.ParseQueryString(handler.Last!.RequestUri!.Query);
        Assert.Equal("1", q["reconnect"]);
        Assert.Equal("call-1", q["call_id"]);
        Assert.Equal("meeting", q["kind"]);
    }

    [Fact]
    public async Task Speakers_post_goes_to_the_session_url_with_the_bearer_and_reports_failure()
    {
        var (client, handler) = Client();
        var report = new SpeakersReport("speakers", "call-1", 5, 500, Array.Empty<SpeakerEntry>());
        Assert.True(await client.PostSpeakersAsync(report, CancellationToken.None));
        Assert.Equal(HttpMethod.Post, handler.Last!.Method);
        Assert.Equal("https://agents.example/ada/voice/session", handler.Last.RequestUri!.ToString());
        Assert.Equal("tok-0123456789", handler.Last.Headers.Authorization!.Parameter);
        Assert.Equal("""{"type":"speakers","call_id":"call-1","at_ms":5,"window_ms":500,"speakers":[]}""", handler.Body);

        handler.Status = HttpStatusCode.ServiceUnavailable;
        Assert.False(await client.PostSpeakersAsync(report, CancellationToken.None));
    }
}
