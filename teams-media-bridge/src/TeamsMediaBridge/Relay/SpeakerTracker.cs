using System.Buffers.Binary;
using System.Text.Json.Serialization;
using Microsoft.Graph.Models;

namespace TeamsMediaBridge.Relay;

/// <summary>PCM16 (little-endian, mono) helpers for the unmixed meeting streams.</summary>
public static class Pcm16
{
    /// <summary>Root-mean-square level of a PCM16 frame, 0..32768. 0 for an empty frame.</summary>
    public static double Rms(ReadOnlySpan<byte> pcm)
    {
        var samples = pcm.Length / 2;
        if (samples == 0)
        {
            return 0;
        }

        double sum = 0;
        for (var i = 0; i < samples; i++)
        {
            double s = BinaryPrimitives.ReadInt16LittleEndian(pcm.Slice(i * 2, 2));
            sum += s * s;
        }

        return Math.Sqrt(sum / samples);
    }

    /// <summary>
    /// Sum the streams sample by sample, clipped to the int16 range, into one frame of <paramref name="frameBytes"/>.
    /// Shorter streams count as silence for their missing tail; longer ones are cut to the frame.
    /// </summary>
    public static byte[] Mix(IReadOnlyList<byte[]> streams, int frameBytes)
    {
        var mixed = new byte[frameBytes];
        var samples = frameBytes / 2;
        for (var i = 0; i < samples; i++)
        {
            var sum = 0;
            foreach (var s in streams)
            {
                if ((i * 2) + 1 < s.Length)
                {
                    sum += BinaryPrimitives.ReadInt16LittleEndian(s.AsSpan(i * 2, 2));
                }
            }

            BinaryPrimitives.WriteInt16LittleEndian(mixed.AsSpan(i * 2, 2), (short)Math.Clamp(sum, short.MinValue, short.MaxValue));
        }

        return mixed;
    }
}

/// <summary>Who an audio source id (MSI) belongs to.</summary>
public sealed record SpeakerIdentity(string AadId, string Name);

/// <summary>
/// MSI → participant, rebuilt from the call roster on every participant update. Each participant's audio
/// media stream carries its MSI as <c>SourceId</c>; the identity is <c>Info.Identity</c> (User for Entra users,
/// Guest / Phone / ApplicationInstance otherwise). Only User ids are passed on as aad_id.
/// </summary>
public sealed class SpeakerRoster
{
    private volatile IReadOnlyDictionary<uint, SpeakerIdentity> _map = new Dictionary<uint, SpeakerIdentity>();

    public int Count => _map.Count;

    public bool TryGet(uint msi, out SpeakerIdentity identity)
    {
        if (_map.TryGetValue(msi, out var found))
        {
            identity = found;
            return true;
        }

        identity = new SpeakerIdentity("", "");
        return false;
    }

    /// <summary>Replace the map with the current roster (people who left drop out).</summary>
    public void Update(IEnumerable<Participant?> participants) => _map = Build(participants);

    public static Dictionary<uint, SpeakerIdentity> Build(IEnumerable<Participant?> participants)
    {
        var map = new Dictionary<uint, SpeakerIdentity>();
        foreach (var p in participants)
        {
            if (p?.MediaStreams is null)
            {
                continue;
            }

            var id = p.Info?.Identity;
            var c = id as CommunicationsIdentitySet;
            var aad = id?.User?.Id ?? "";
            var name = id?.User?.DisplayName ?? c?.Guest?.DisplayName ?? c?.Phone?.DisplayName
                ?? c?.ApplicationInstance?.DisplayName ?? id?.Application?.DisplayName ?? "";
            foreach (var stream in p.MediaStreams)
            {
                if (stream?.MediaType == Modality.Audio && uint.TryParse(stream.SourceId, out var msi))
                {
                    map[msi] = new SpeakerIdentity(aad, name);
                }
            }
        }

        return map;
    }
}

/// <summary>One speaker's share of a report window.</summary>
public sealed record SpeakerEntry(
    [property: JsonPropertyName("msi")] uint Msi,
    [property: JsonPropertyName("aad_id")] string AadId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("speech_ms")] int SpeechMs);

/// <summary>The body POSTed to the agent's session URL: who spoke in the last window (empty = silence after speech).</summary>
public sealed record SpeakersReport(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("at_ms")] long AtMs,
    [property: JsonPropertyName("window_ms")] int WindowMs,
    [property: JsonPropertyName("speakers")] IReadOnlyList<SpeakerEntry> Speakers);

/// <summary>
/// Per-speaker voice activity, aggregated into fixed windows. The media thread feeds frames (<see cref="AddLevel"/>
/// for unmixed streams with an energy VAD, <see cref="AddActive"/> for the platform's ActiveSpeakers list); a timer
/// calls <see cref="Flush"/> once per window. A frame counts as speech above the RMS threshold, and for the
/// hangover after it, so short dips between syllables don't split a talker. Times are caller-supplied ms.
/// </summary>
public sealed class SpeakerTracker
{
    private readonly object _lock = new();
    private readonly double _threshold;
    private readonly int _hangoverMs;
    private readonly int _silenceMs;
    private readonly Dictionary<uint, long> _lastLoudAt = new();
    private readonly Dictionary<uint, int> _window = new();
    private long _lastSpeechAt = long.MinValue;
    private bool _silenceOwed;

    public SpeakerTracker(double rmsThreshold, int hangoverMs, int windowMs, int silenceMs)
    {
        _threshold = rmsThreshold;
        _hangoverMs = hangoverMs;
        WindowMs = windowMs;
        _silenceMs = silenceMs;
    }

    public int WindowMs { get; }

    /// <summary>One unmixed frame from <paramref name="msi"/> with level <paramref name="rms"/>. Returns true if it counted as speech.</summary>
    public bool AddLevel(uint msi, double rms, int frameMs, long nowMs)
    {
        lock (_lock)
        {
            if (rms >= _threshold)
            {
                _lastLoudAt[msi] = nowMs;
                _lastSpeechAt = nowMs;
            }
            else if (!_lastLoudAt.TryGetValue(msi, out var loud) || nowMs - loud > _hangoverMs)
            {
                return false;
            }

            _window[msi] = _window.GetValueOrDefault(msi) + frameMs;
            return true;
        }
    }

    /// <summary>Fallback: the platform says these MSIs are active in this frame (it leaves out silent ones).</summary>
    public void AddActive(IEnumerable<uint> msis, int frameMs, long nowMs)
    {
        lock (_lock)
        {
            foreach (var msi in msis)
            {
                _window[msi] = _window.GetValueOrDefault(msi) + frameMs;
                _lastSpeechAt = nowMs;
            }
        }
    }

    /// <summary>
    /// Close the window ending at <paramref name="nowMs"/>. Returns the speakers if anyone spoke; once per silence,
    /// an empty list when everyone has been quiet for the silence time after speech; otherwise null.
    /// </summary>
    public SpeakersReport? Flush(string callId, long nowMs, long unixMs, SpeakerRoster roster)
    {
        lock (_lock)
        {
            if (_window.Count > 0)
            {
                var speakers = _window
                    .OrderByDescending(kv => kv.Value)
                    .Select(kv => new SpeakerEntry(kv.Key, "", "", Math.Min(kv.Value, WindowMs)))
                    .ToList();
                _window.Clear();
                _silenceOwed = true;
                return Resolve(new SpeakersReport("speakers", callId, unixMs, WindowMs, speakers), roster);
            }

            if (_silenceOwed && nowMs - _lastSpeechAt >= _silenceMs)
            {
                _silenceOwed = false;
                return new SpeakersReport("speakers", callId, unixMs, WindowMs, Array.Empty<SpeakerEntry>());
            }

            return null;
        }
    }

    /// <summary>Fill in aad_id and name for each MSI the roster knows.</summary>
    public static SpeakersReport Resolve(SpeakersReport report, SpeakerRoster roster) => report with
    {
        Speakers = report.Speakers
            .Select(e => roster.TryGet(e.Msi, out var who) ? e with { AadId = who.AadId, Name = who.Name } : e)
            .ToList(),
    };

    /// <summary>True if some speaker in the report is not (yet) on the roster.</summary>
    public static bool HasUnknown(SpeakersReport report) => report.Speakers.Any(e => e.AadId.Length == 0 && e.Name.Length == 0);
}
