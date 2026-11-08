using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Skype.Bots.Media;

namespace TeamsMediaBridge.Relay;

/// <summary>
/// Agent → Teams playback pacer. The AudioSocket wants exactly one 20 ms PCM16K frame (640 bytes) per Send,
/// 50 per second (IAudioSocket.Send docs: "The application should be sending 50 frames of audio media per
/// second with each frame containing 20 milliseconds of audio content").
///
/// A dedicated thread ticks every 20 ms and tops the socket's own send queue up to a small depth
/// (IAudioSocket.GetSendBuffersPendingCount), so timer jitter never causes underruns, while a barge-in can only
/// ever leave SocketQueueFrames × 20 ms of stale audio playing. <see cref="Clear"/> drops everything we hold, instantly.
/// </summary>
public sealed class AudioPlayout : IDisposable
{
    public const int FrameBytes = FrameQueue.FrameBytes;

    private readonly FrameQueue _queue = new();
    private readonly IAudioSocket _socket;
    private readonly int _socketDepth;
    private readonly ILogger _logger;
    private readonly Thread _thread;
    private volatile bool _sendActive;
    private volatile bool _stopping;

    /// <summary>Raised on the playout thread when the first frame of a new agent utterance is handed to Teams.</summary>
    public event Action? UtteranceStarted;

    public AudioPlayout(IAudioSocket socket, int socketDepthFrames, ILogger logger)
    {
        _socket = socket;
        _socketDepth = Math.Max(1, socketDepthFrames);
        _logger = logger;
        _socket.AudioSendStatusChanged += OnSendStatusChanged;
        _thread = new Thread(Run) { IsBackground = true, Name = "audio-playout", Priority = ThreadPriority.AboveNormal };
        _thread.Start();
    }

    /// <summary>Milliseconds of agent audio still held here (excludes the few frames already inside the socket).</summary>
    public int QueuedMs => _queue.QueuedMs;

    public void Append(ReadOnlySpan<byte> pcm16k) => _queue.Append(pcm16k);

    /// <summary>Barge-in: drop everything not yet handed to the socket. Returns the ms dropped.</summary>
    public int Clear() => _queue.Clear();

    private void OnSendStatusChanged(object? sender, AudioSendStatusChangedEventArgs e)
    {
        // Frames sent before the socket reports Active are discarded by the platform.
        _sendActive = e.MediaSendStatus == MediaSendStatus.Active;
        _logger.LogInformation("Audio send status {Status}", e.MediaSendStatus);
    }

    private void Run()
    {
        // Default Windows timer resolution is ~15.6 ms, too coarse for a 20 ms cadence.
        var raisedTimerResolution = OperatingSystem.IsWindows() && TimeBeginPeriod(1) == 0;
        try
        {
            var frameTicks = Stopwatch.Frequency * FrameQueue.FrameMs / 1000;
            var next = Stopwatch.GetTimestamp();
            while (!_stopping)
            {
                next += frameTicks;
                var now = Stopwatch.GetTimestamp();
                if (now - next > 5 * frameTicks)
                {
                    next = now; // fell far behind (GC pause, VM steal): resync rather than burst
                }

                var waitMs = (int)((next - now) * 1000 / Stopwatch.Frequency);
                if (waitMs > 1)
                {
                    Thread.Sleep(waitMs - 1);
                }

                while (Stopwatch.GetTimestamp() < next)
                {
                    Thread.SpinWait(50);
                }

                try
                {
                    Tick();
                }
                catch (Exception ex) when (!_stopping)
                {
                    _logger.LogWarning(ex, "Playout tick failed");
                }
            }
        }
        finally
        {
            if (raisedTimerResolution)
            {
                TimeEndPeriod(1);
            }
        }
    }

    private void Tick()
    {
        if (!_sendActive)
        {
            return;
        }

        var pending = _socket.GetSendBuffersPendingCount();
        while (pending < _socketDepth && _queue.TryTake(out var frame, out var startsUtterance))
        {
            var ptr = Marshal.AllocHGlobal(FrameBytes);
            Marshal.Copy(frame, 0, ptr, FrameBytes);
            // AudioSendBuffer.Dispose() (called by the platform when done) frees the HGlobal.
            // Timestamp should come from MediaPlatform.GetCurrentTimestamp() (AudioMediaBuffer.Timestamp docs).
            _socket.Send(new AudioSendBuffer(ptr, FrameBytes, AudioFormat.Pcm16K, MediaPlatform.GetCurrentTimestamp()));
            pending++;
            if (startsUtterance)
            {
                UtteranceStarted?.Invoke();
            }
        }
    }

    public void Dispose()
    {
        _stopping = true;
        _socket.AudioSendStatusChanged -= OnSendStatusChanged;
        _thread.Join(TimeSpan.FromSeconds(1));
        _queue.Clear();
    }

    [DllImport("winmm.dll", EntryPoint = "timeBeginPeriod")]
    private static extern uint TimeBeginPeriod(uint uMilliseconds);

    [DllImport("winmm.dll", EntryPoint = "timeEndPeriod")]
    private static extern uint TimeEndPeriod(uint uMilliseconds);
}
