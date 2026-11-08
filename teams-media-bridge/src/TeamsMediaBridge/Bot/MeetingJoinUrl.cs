using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Graph.Contracts;
using Microsoft.Graph.Models;

namespace TeamsMediaBridge.Bot;

/// <summary>Parses a Teams meeting join link into the Graph ChatInfo/MeetingInfo pair needed to join it.</summary>
public static partial class MeetingJoinUrl
{
    public sealed record Parsed(ChatInfo? ChatInfo, MeetingInfo MeetingInfo, string? TenantId);

    // Classic link: https://teams.microsoft.com/l/meetup-join/<thread>/<message>?context={"Tid":"..","Oid":".."}
    // Regex and context shape are from the EchoBot sample's Models/JoinInfo.cs.
    [GeneratedRegex("https://teams\\.microsoft\\.com.*/(?<thread>[^/]+)/(?<message>[^/]+)\\?context=(?<context>{.*})")]
    private static partial Regex ClassicLink();

    // Newer short link: https://teams.microsoft.com/meet/<joinMeetingId>?p=<passcode>
    [GeneratedRegex("https://teams\\.microsoft\\.com/meet/(?<id>[0-9 ]+)(\\?p=(?<passcode>[^&]+))?", RegexOptions.IgnoreCase)]
    private static partial Regex ShortLink();

    private sealed record Context(string? Tid, string? Oid, string? MessageId);

    public static Parsed Parse(string joinUrl)
    {
        if (string.IsNullOrWhiteSpace(joinUrl))
        {
            throw new ArgumentException("joinUrl is required", nameof(joinUrl));
        }

        var decoded = WebUtility.UrlDecode(joinUrl.Trim());

        var classic = ClassicLink().Match(decoded);
        if (classic.Success)
        {
            var ctx = JsonSerializer.Deserialize<Context>(
                classic.Groups["context"].Value,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
                ?? throw new ArgumentException("Join URL context is empty", nameof(joinUrl));
            if (string.IsNullOrEmpty(ctx.Tid))
            {
                throw new ArgumentException("Join URL is invalid: missing Tid", nameof(joinUrl));
            }

            var chatInfo = new ChatInfo
            {
                ThreadId = classic.Groups["thread"].Value,
                MessageId = classic.Groups["message"].Value,
                ReplyChainMessageId = ctx.MessageId,
            };
            var meetingInfo = new OrganizerMeetingInfo
            {
                Organizer = new IdentitySet { User = new Identity { Id = ctx.Oid } },
            };
            meetingInfo.Organizer.User.SetTenantId(ctx.Tid);
            return new Parsed(chatInfo, meetingInfo, ctx.Tid);
        }

        var shortLink = ShortLink().Match(decoded);
        if (shortLink.Success)
        {
            // TODO(phase 2, untested): Graph supports joining by joinMeetingId + passcode
            // (https://learn.microsoft.com/en-us/graph/api/resources/joinmeetingidmeetinginfo), but the
            // comms samples only exercise the classic link. Verify against a real meeting before relying on it.
            var meetingInfo = new JoinMeetingIdMeetingInfo
            {
                JoinMeetingId = shortLink.Groups["id"].Value.Replace(" ", string.Empty),
                Passcode = shortLink.Groups["passcode"].Success ? shortLink.Groups["passcode"].Value : null,
            };
            // JoinMeetingParameters accepts a null ChatInfo (only MeetingInfo and the media session are validated).
            return new Parsed(null, meetingInfo, null);
        }

        throw new ArgumentException("Unrecognised Teams join URL", nameof(joinUrl));
    }
}
