using System.Diagnostics;

namespace TeamsMediaBridge.Relay;

/// <summary>
/// Thread-safe re-framer for agent audio: arbitrary-size PCM16 16 kHz mono chunks in, exact 640-byte
/// (20 ms) frames out. A trailing partial frame is held until more audio arrives, or padded with silence once
/// no audio has arrived for <see cref="FlushPartialAfterMs"/>. Pure managed code (no media SDK) so it is unit-testable.
/// </summary>
public sealed class FrameQueue
{
    public const int FrameBytes = 640;              // 20 ms × 16 000 Hz × 2 bytes
    public const int FrameMs = 20;
    public const int FlushPartialAfterMs = 60;      // quiet this long → pad and release the tail
    public const int NewUtteranceGapMs = 300;       // silence longer than this before audio = new agent utterance

    private readonly object _lock = new();
    private readonly Queue<byte[]> _frames = new();
    private readonly byte[] _partial = new byte[FrameBytes];
    private readonly Func<long> _clock;
    private int _partialLength;
    private long _lastAppend;
    private long _lastTaken;
    private bool _idle = true;

    /// <param name="clock">Stopwatch-tick clock; injectable for tests.</param>
    public FrameQueue(Func<long>? clock = null) => _clock = clock ?? Stopwatch.GetTimestamp;

    /// <summary>Milliseconds of audio held (a partial frame counts as one frame).</summary>
    public int QueuedMs
    {
        get { lock (_lock) { return HeldMs(); } }
    }

    public void Append(ReadOnlySpan<byte> pcm)
    {
        lock (_lock)
        {
            while (!pcm.IsEmpty)
            {
                var take = Math.Min(FrameBytes - _partialLength, pcm.Length);
                pcm[..take].CopyTo(_partial.AsSpan(_partialLength));
                _partialLength += take;
                pcm = pcm[take..];
                if (_partialLength == FrameBytes)
                {
                    _frames.Enqueue((byte[])_partial.Clone());
                    _partialLength = 0;
                }
            }

            _lastAppend = _clock();
        }
    }

    /// <summary>Barge-in: drop everything. Returns the ms dropped.</summary>
    public int Clear()
    {
        lock (_lock)
        {
            var dropped = HeldMs();
            _frames.Clear();
            _partialLength = 0;
            _idle = true;
            _lastTaken = 0; // audio after a barge-in always counts as a new utterance
            return dropped;
        }
    }

    /// <summary>Take the next whole frame, if any. <paramref name="startsUtterance"/> marks the first frame after a gap.</summary>
    public bool TryTake(out byte[] frame, out bool startsUtterance)
    {
        lock (_lock)
        {
            startsUtterance = false;
            var now = _clock();
            if (_frames.Count == 0 && _partialLength > 0 && ElapsedMs(_lastAppend, now) >= FlushPartialAfterMs)
            {
                Array.Clear(_partial, _partialLength, FrameBytes - _partialLength); // pad with silence
                _frames.Enqueue((byte[])_partial.Clone());
                _partialLength = 0;
            }

            if (_frames.Count == 0)
            {
                _idle = true;
                frame = Array.Empty<byte>();
                return false;
            }

            frame = _frames.Dequeue();
            startsUtterance = _idle && (_lastTaken == 0 || ElapsedMs(_lastTaken, now) >= NewUtteranceGapMs);
            _idle = false;
            _lastTaken = now;
            return true;
        }
    }

    private int HeldMs() => (_frames.Count * FrameMs) + (_partialLength > 0 ? FrameMs : 0);

    private static double ElapsedMs(long from, long to) => (to - from) * 1000.0 / Stopwatch.Frequency;
}
