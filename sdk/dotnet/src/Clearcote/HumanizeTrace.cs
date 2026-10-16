using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace Clearcote;

/// <summary>
/// Humanize score hook (SC 232 lane G; mirrors sdk/python/clearcote/_trace.py and sdk/node/src/trace.ts).
/// <c>CLEARCOTE_HUMANIZE_SCORE=1</c> makes the SDK record every action it dispatches, score it with
/// <see cref="MotionScore"/> and append one JSON line per action to <c>CLEARCOTE_HUMANIZE_SCORE_FILE</c>
/// (default <c>./clearcote-humanize-score.jsonl</c>). A score below <see cref="WarnBelow"/> prints a
/// <c>clearcote: warning:</c> line naming the penalties, once per kind + penalty set.
///
/// Read-only tap: nothing here changes what is dispatched or when, and <c>Record</c> never throws. Callers:
/// <see cref="Pacing.WalkAsync"/> (every walked pointer path, actual dispatch times); the typing loop
/// (<c>Record("typed", keys)</c>), the wheel loop (<c>Record("wheel", events)</c>) and hover dwell
/// (<c>Record("hover", samplesFromArrival)</c>) as the lanes that own those loops add the calls.
/// </summary>
public static class HumanizeTrace
{
    public const string ScoreEnv = "CLEARCOTE_HUMANIZE_SCORE";
    public const string ScoreFileEnv = "CLEARCOTE_HUMANIZE_SCORE_FILE";
    public const string DefaultScoreFile = "./clearcote-humanize-score.jsonl";
    public const double WarnBelow = 0.7;

    private static readonly object Gate = new();
    private static readonly string[] Off = { "", "0", "false", "off", "no" };

    /// <summary>True when the hook is on (CLEARCOTE_HUMANIZE_SCORE set to anything but 0/false/off/no).</summary>
    public static bool Enabled => !Off.Contains((Environment.GetEnvironmentVariable(ScoreEnv) ?? "").Trim().ToLowerInvariant());

    public static string ScoreFile
    {
        get { var f = Environment.GetEnvironmentVariable(ScoreFileEnv); return string.IsNullOrEmpty(f) ? DefaultScoreFile : f; }
    }

    public static ActionScore? Record(string kind, IReadOnlyList<MotionSample>? samples, IReadOnlyDictionary<string, object?>? meta = null) =>
        Enabled && samples is { Count: > 0 } ? Write(() => MotionScore.ScoreAction(kind, samples), samples.Count, meta) : null;

    public static ActionScore? Record(string kind, IReadOnlyList<KeySample>? samples, IReadOnlyDictionary<string, object?>? meta = null) =>
        Enabled && samples is { Count: > 0 } ? Write(() => MotionScore.ScoreAction(kind, samples), samples.Count, meta) : null;

    public static ActionScore? Record(string kind, IReadOnlyList<WheelSample>? samples, IReadOnlyDictionary<string, object?>? meta = null) =>
        Enabled && samples is { Count: > 0 } ? Write(() => MotionScore.ScoreAction(kind, samples), samples.Count, meta) : null;

    private static object? R4(object? v) => v switch
    {
        double d => double.IsFinite(d) ? Math.Round(d, 4) : null,
        IReadOnlyDictionary<string, object?> m => m.ToDictionary(kv => kv.Key, kv => R4(kv.Value)),
        _ => v,
    };

    private static ActionScore? Write(Func<ActionScore> score, int n, IReadOnlyDictionary<string, object?>? meta)
    {
        try
        {
            var r = score();
            var line = new Dictionary<string, object?>
            {
                ["t"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
                ["pid"] = Environment.ProcessId,
                ["kind"] = r.Kind,
                ["n"] = n,
                ["score"] = r.Score,
                ["flags"] = r.Flags,
                ["features"] = r.Features.ToDictionary(kv => kv.Key, kv => R4(kv.Value)),
            };
            if (meta != null) line["meta"] = meta.ToDictionary(kv => kv.Key, kv => R4(kv.Value));
            var json = JsonSerializer.Serialize(line);
            lock (Gate) File.AppendAllText(ScoreFile, json + "\n");
            if (r.Score < WarnBelow)
            {
                var names = r.Flags.Count > 0 ? string.Join(", ", r.Flags.Select(f => f.Split('(')[0])) : "none";
                LaunchWarnings.EmitOnce(new[] { new LaunchWarnings.Warning($"humanize-score:{r.Kind}:{names}",
                    $"humanize {r.Kind} scored {r.Score.ToString("F2", System.Globalization.CultureInfo.InvariantCulture)} (< {WarnBelow.ToString(System.Globalization.CultureInfo.InvariantCulture)}): {names}") }, false);
            }
            return r;
        }
        catch
        {
            return null;   // instrumentation must never break the action
        }
    }
}
