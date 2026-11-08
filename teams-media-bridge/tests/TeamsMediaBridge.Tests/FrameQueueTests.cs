using System.Diagnostics;
using TeamsMediaBridge.Relay;
using Xunit;

namespace TeamsMediaBridge.Tests;

public class FrameQueueTests
{
    private long _now = 1;
    private FrameQueue NewQueue() => new(() => _now);
    private void AdvanceMs(int ms) => _now += Stopwatch.Frequency * ms / 1000;

    [Fact]
    public void Slices_arbitrary_chunks_into_640_byte_frames_in_order()
    {
        var q = NewQueue();
        var pcm = Enumerable.Range(0, 1000).Select(i => (byte)i).ToArray();
        q.Append(pcm.AsSpan(0, 300));
        q.Append(pcm.AsSpan(300, 700)); // 1000 bytes total → 1 frame + 360 byte remainder

        Assert.True(q.TryTake(out var frame, out _));
        Assert.Equal(640, frame.Length);
        Assert.Equal(pcm[..640], frame);
        Assert.False(q.TryTake(out _, out _)); // remainder is held, not padded, while audio may still come
    }

    [Fact]
    public void Pads_the_tail_with_silence_once_audio_stops()
    {
        var q = NewQueue();
        q.Append(Enumerable.Repeat((byte)7, 100).ToArray());
        Assert.False(q.TryTake(out _, out _));

        AdvanceMs(FrameQueue.FlushPartialAfterMs);
        Assert.True(q.TryTake(out var frame, out _));
        Assert.Equal(640, frame.Length);
        Assert.All(frame[..100], b => Assert.Equal(7, b));
        Assert.All(frame[100..], b => Assert.Equal(0, b));
    }

    [Fact]
    public void Clear_drops_everything_and_reports_ms()
    {
        var q = NewQueue();
        q.Append(new byte[640 * 10 + 5]); // 10 frames + partial
        Assert.Equal(220, q.QueuedMs);

        Assert.Equal(220, q.Clear());
        Assert.Equal(0, q.QueuedMs);
        AdvanceMs(1000);
        Assert.False(q.TryTake(out _, out _));
    }

    [Fact]
    public void Marks_utterance_start_only_after_a_gap_or_barge_in()
    {
        var q = NewQueue();
        q.Append(new byte[640 * 2]);
        Assert.True(q.TryTake(out _, out var first));
        Assert.True(first);
        Assert.True(q.TryTake(out _, out var second));
        Assert.False(second);

        // Brief underrun mid-utterance (network jitter) is not a new utterance.
        Assert.False(q.TryTake(out _, out _));
        AdvanceMs(40);
        q.Append(new byte[640]);
        Assert.True(q.TryTake(out _, out var afterJitter));
        Assert.False(afterJitter);

        // A long pause is.
        AdvanceMs(FrameQueue.NewUtteranceGapMs + 10);
        Assert.False(q.TryTake(out _, out _));
        q.Append(new byte[640]);
        Assert.True(q.TryTake(out _, out var afterPause));
        Assert.True(afterPause);

        // And so is the first audio after a barge-in, even with no gap.
        q.Append(new byte[640 * 3]);
        q.TryTake(out _, out _);
        q.Clear();
        q.Append(new byte[640]);
        Assert.True(q.TryTake(out _, out var afterBargeIn));
        Assert.True(afterBargeIn);
    }
}
