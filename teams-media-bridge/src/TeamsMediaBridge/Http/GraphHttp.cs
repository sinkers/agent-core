using System.Security.Cryptography;
using System.Text;

namespace TeamsMediaBridge.Http;

/// <summary>
/// Glue between ASP.NET Core and the Graph Communications SDK, which consumes/produces
/// System.Net.Http messages (ICommunicationsClient.ProcessNotificationAsync).
/// </summary>
public static class GraphHttp
{
    /// <summary>
    /// Build an HttpRequestMessage from the incoming request. Based on the EchoBot sample's HttpHelpers,
    /// with the fix from the ElevenLabs guide: Host.Port is null when the client used the default
    /// port (443), so fall back instead of calling .Value.
    /// https://elevenlabs.io/docs/eleven-agents/phone-numbers/microsoft-teams/graph-media-bot (Step 3)
    /// </summary>
    public static HttpRequestMessage ToHttpRequestMessage(this HttpRequest req)
    {
        var uri = new UriBuilder
        {
            Scheme = req.Scheme,
            Host = req.Host.Host,
            Port = req.Host.Port ?? (req.IsHttps ? 443 : 80),
            Path = req.PathBase.Add(req.Path),
            Query = req.QueryString.ToString(),
        }.Uri;

        var msg = new HttpRequestMessage(new HttpMethod(req.Method), uri)
        {
            Content = new StreamContent(req.Body),
        };

        foreach (var header in req.Headers)
        {
            // Content headers must go on Content; everything else on the request.
            if (!msg.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable()))
            {
                msg.Content.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable());
            }
        }

        return msg;
    }

    /// <summary>Copy an SDK-produced HttpResponseMessage onto the ASP.NET Core response.</summary>
    public static async Task CopyToAsync(this HttpResponseMessage source, HttpResponse target)
    {
        target.StatusCode = (int)source.StatusCode;
        foreach (var h in source.Headers)
        {
            target.Headers[h.Key] = h.Value.ToArray();
        }

        if (source.Content is not null)
        {
            foreach (var h in source.Content.Headers)
            {
                target.Headers[h.Key] = h.Value.ToArray();
            }

            // Kestrel sets these itself.
            target.Headers.Remove("transfer-encoding");
            await source.Content.CopyToAsync(target.Body).ConfigureAwait(false);
        }
    }

    /// <summary>Constant-time check of "Authorization: Bearer &lt;token&gt;".</summary>
    public static bool HasBearer(this HttpRequest req, string expectedToken)
    {
        var header = req.Headers.Authorization.ToString();
        const string prefix = "Bearer ";
        if (string.IsNullOrEmpty(expectedToken) || !header.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        var presented = Encoding.UTF8.GetBytes(header[prefix.Length..].Trim());
        var expected = Encoding.UTF8.GetBytes(expectedToken);
        return CryptographicOperations.FixedTimeEquals(presented, expected);
    }
}
