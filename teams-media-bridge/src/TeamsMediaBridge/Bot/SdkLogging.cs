using System.Diagnostics;
using Microsoft.Graph.Communications.Common.Telemetry;
using MediaLogLevel = Microsoft.Skype.Bots.Media.LogLevel;

namespace TeamsMediaBridge.Bot;

/// <summary>Routes the Real-time Media Platform's own log lines into ILogger (same as EchoBot's BotMediaLogger).</summary>
public sealed class MediaPlatformLogger : Microsoft.Skype.Bots.Media.IMediaPlatformLogger
{
    private readonly ILogger _logger;

    public MediaPlatformLogger(ILogger<MediaPlatformLogger> logger) => _logger = logger;

    public void WriteLog(MediaLogLevel level, string logStatement)
    {
        var mapped = level switch
        {
            MediaLogLevel.Error => LogLevel.Error,
            MediaLogLevel.Warning => LogLevel.Warning,
            MediaLogLevel.Information => LogLevel.Information,
            _ => LogLevel.Trace,
        };
        _logger.Log(mapped, "[media] {Statement}", logStatement);
    }
}

/// <summary>
/// Subscribes to the Graph Communications SDK's IGraphLogger and forwards warnings/errors (and info at Debug)
/// into ILogger. Metrics and HTTP traces are dropped: they are very chatty.
/// </summary>
public sealed class GraphLogObserver : IObserver<LogEvent>, IDisposable
{
    private readonly ILogger _logger;
    private readonly IDisposable _subscription;

    public GraphLogObserver(IGraphLogger graphLogger, ILogger<GraphLogObserver> logger)
    {
        _logger = logger;
        _subscription = graphLogger.Subscribe(this);
    }

    public void OnNext(LogEvent e)
    {
        if (e.EventType is LogEventType.Metric or LogEventType.HttpTrace)
        {
            return;
        }

        var level = e.Level switch
        {
            TraceLevel.Error => LogLevel.Error,
            TraceLevel.Warning => LogLevel.Warning,
            TraceLevel.Info => LogLevel.Debug,
            _ => LogLevel.Trace,
        };
        _logger.Log(level, "[graph] {Message}", e.Message);
    }

    public void OnError(Exception error) => _logger.LogError(error, "[graph] logger error");

    public void OnCompleted()
    {
    }

    public void Dispose() => _subscription.Dispose();
}
