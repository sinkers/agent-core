// Teams real-time media bridge: Teams (Graph application-hosted media, Windows) ⇄ ElevenLabs Agents (WebSocket).
// A thin relay: no turn detection, STT, TTS or LLM here. See README.md.
using Microsoft.Extensions.Options;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;
using Serilog;
using Serilog.Extensions.Logging;
using TeamsMediaBridge;
using TeamsMediaBridge.Bot;
using TeamsMediaBridge.Http;
using TeamsMediaBridge.Relay;

Log.Logger = new LoggerConfiguration().WriteTo.Console().CreateBootstrapLogger();

if (!OperatingSystem.IsWindows())
{
    // Microsoft.Skype.Bots.Media is native Windows x64 only; building elsewhere is fine, running is not.
    Log.Fatal("The Real-time Media Platform only runs on Windows Server x64.");
    return 1;
}

try
{
    var builder = WebApplication.CreateBuilder(new WebApplicationOptions
    {
        Args = args,
        // A Windows service starts in C:\Windows\System32; load appsettings.json from the install folder.
        ContentRootPath = AppContext.BaseDirectory,
    });

    // Sources (later wins): appsettings.json → appsettings.{Environment}.json → BRIDGE_CONFIG json file
    // (default C:\teams-media-bridge\config\bridge.json, written by install.ps1, ACL'd to SYSTEM/Administrators,
    // outside the app folder so redeploys don't wipe it) → environment variables (Bot__AppSecret, ...).
    var bridgeConfig = Environment.GetEnvironmentVariable("BRIDGE_CONFIG") ?? @"C:\teams-media-bridge\config\bridge.json";
    builder.Configuration.AddJsonFile(bridgeConfig, optional: true, reloadOnChange: false);
    builder.Configuration.AddEnvironmentVariables();
    builder.Host.UseWindowsService(o => o.ServiceName = "TeamsMediaBridge");
    builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(15));

    var logOptions = builder.Configuration.GetSection(LogOptions.Section).Get<LogOptions>() ?? new LogOptions();
    builder.Host.UseSerilog((ctx, _, cfg) => cfg
        .ReadFrom.Configuration(ctx.Configuration)
        .Enrich.FromLogContext()
        .WriteTo.Console()
        .WriteTo.File(
            Path.Combine(logOptions.Directory, "bridge-.log"),
            rollingInterval: RollingInterval.Day,
            retainedFileCountLimit: logOptions.RetainedDays,
            fileSizeLimitBytes: 100 * 1024 * 1024,
            rollOnFileSizeLimit: true,
            shared: true));

    builder.Services.AddOptions<BotOptions>().BindConfiguration(BotOptions.Section).ValidateDataAnnotations().ValidateOnStart();
    builder.Services.AddOptions<AgentOptions>().BindConfiguration(AgentOptions.Section).ValidateDataAnnotations().ValidateOnStart();
    builder.Services.AddOptions<RelayOptions>().BindConfiguration(RelayOptions.Section).ValidateDataAnnotations().ValidateOnStart();

    builder.Services.AddHttpClient(AgentSessionClient.HttpClientName);
    builder.Services.AddSingleton<AgentSessionClient>();
    builder.Services.AddSingleton<IGraphLogger>(_ => new GraphLogger("TeamsMediaBridge", redirectToTrace: false));
    builder.Services.AddSingleton<GraphLogObserver>();
    builder.Services.AddSingleton<MediaPlatformLogger>();
    builder.Services.AddSingleton<BotService>();

    // One HTTPS listener for Graph callbacks + our API, using the same CA-signed cert as the media platform.
    var botOptions = builder.Configuration.GetSection(BotOptions.Section).Get<BotOptions>() ?? new BotOptions();
    var startupLogger = new SerilogLoggerFactory(Log.Logger).CreateLogger("Startup");
    var certificate = BotService.FindCertificate(botOptions, startupLogger);
    builder.WebHost.ConfigureKestrel(k => k.ListenAnyIP(botOptions.CallSignalingPort, l => l.UseHttps(certificate)));

    var app = builder.Build();

    _ = app.Services.GetRequiredService<GraphLogObserver>();
    var bot = app.Services.GetRequiredService<BotService>();
    bot.Initialize(certificate);
    app.Lifetime.ApplicationStopping.Register(() => bot.DisposeAsync().AsTask().Wait(TimeSpan.FromSeconds(10)));

    // Graph calling webhook (set on the Azure Bot's Teams channel as https://<fqdn>/api/calling) and the
    // per-call notification URL. The SDK validates Graph's JWT inside ProcessNotificationAsync.
    async Task Notify(HttpContext ctx)
    {
        using var request = ctx.Request.ToHttpRequestMessage();
        using var response = await bot.Client.ProcessNotificationAsync(request).ConfigureAwait(false);
        await response.CopyToAsync(ctx.Response).ConfigureAwait(false);
    }

    app.MapPost(BotService.CallbackRoute, Notify);
    app.MapPost(BotService.NotificationRoute, Notify);

    app.MapGet("/healthz", () => Results.Ok(new { status = "ok", activeCalls = bot.ActiveCalls }));

    // Phase 2: the agent asks the bridge to join a meeting. Same bearer token as the agent session endpoint.
    app.MapPost("/api/join", async (HttpRequest req, JoinRequest body, IOptions<AgentOptions> agentOptions, ILogger<BotService> log) =>
    {
        if (!req.HasBearer(agentOptions.Value.BridgeToken))
        {
            return Results.Unauthorized();
        }

        try
        {
            var callId = await bot.JoinMeetingAsync(body.JoinUrl, body.RequestedBy ?? "");
            return Results.Accepted(value: new { callId });
        }
        catch (ArgumentException ex)
        {
            return Results.BadRequest(new { error = ex.Message });
        }
        catch (Exception ex)
        {
            log.LogError(ex, "Meeting join failed");
            return Results.Problem("join failed", statusCode: 502);
        }
    });

    // The agent swaps the ElevenLabs conversation on a live call for a fresh one (new settings, e.g. speaking speed)
    // without hanging up. Same bearer token. call_id: the id the bridge sent as call_id on the session GET.
    app.MapPost("/api/voice/reconnect", (HttpRequest req, ReconnectRequest body, IOptions<AgentOptions> agentOptions) =>
    {
        if (!req.HasBearer(agentOptions.Value.BridgeToken))
        {
            return Results.Unauthorized();
        }

        return bot.Reconnect(body.CallId) switch
        {
            ReconnectResult.Accepted => Results.Accepted(value: new { reconnected = true }),
            ReconnectResult.NotStarted => Results.Conflict(new { reconnected = false, error = "agent not started yet" }),
            _ => Results.NotFound(new { reconnected = false, error = "no such call" }),
        };
    });

    // The agent rings someone on Teams (cheaper than a phone call). Same bearer token.
    app.MapPost("/api/call", async (HttpRequest req, CallRequest body, IOptions<AgentOptions> agentOptions, ILogger<BotService> log) =>
    {
        if (!req.HasBearer(agentOptions.Value.BridgeToken))
        {
            return Results.Unauthorized();
        }

        try
        {
            var callId = await bot.CallUserAsync(body.UserAadId, body.Ref ?? "");
            return Results.Accepted(value: new { callId });
        }
        catch (ArgumentException ex)
        {
            return Results.BadRequest(new { error = ex.Message });
        }
        catch (InvalidOperationException ex) when (ex.Message == "busy")
        {
            return Results.Problem("all call slots are in use", statusCode: 503);
        }
        catch (Exception ex)
        {
            log.LogError(ex, "Outbound Teams call failed");
            return Results.Problem("call failed", statusCode: 502);
        }
    });

    app.Run();
    return 0;
}
catch (Exception ex)
{
    Log.Fatal(ex, "Bridge terminated unexpectedly");
    return 1;
}
finally
{
    Log.CloseAndFlush();
}

/// <summary>POST /api/join body.</summary>
public sealed record JoinRequest(string JoinUrl, string? RequestedBy);

/// <summary>POST /api/call body: who to ring and the agent's id for the call.</summary>
public sealed record CallRequest(string UserAadId, string? Ref);

/// <summary>POST /api/voice/reconnect body.</summary>
public sealed record ReconnectRequest([property: System.Text.Json.Serialization.JsonPropertyName("call_id")] string? CallId);
