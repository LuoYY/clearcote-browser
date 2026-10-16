using System.Text.Json;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// SC 232 lane G (mirrors sdk/python/tests/test_motionscore_actions.py and node/test/motionscore-actions.test.ts):
/// MotionScore (new in .NET: the detector-emulator existed only in Python and Node) scores every humanize surface,
/// CLEARCOTE_HUMANIZE_SCORE records each walked path, and the three SDKs score the shared traces in
/// sdk/testdata/humanize_motion_vectors.json `scores` identically. On origin/main ceb7983 this file does not
/// compile: MotionScore, HumanizeTrace and the WalkAsync kind parameter do not exist.
/// </summary>
public class MotionScoreTests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_motion_vectors.json"))).RootElement.Clone();

    private static JsonElement Case(string kind, string name) =>
        Vec.GetProperty("scores").EnumerateArray().First(c => c.GetProperty("kind").GetString() == kind && c.GetProperty("name").GetString() == name);

    private static ActionScore Score(string kind, string name) => MotionScore.ScoreActionJson(kind, Case(kind, name).GetProperty("samples"));

    private static IEnumerable<string> Names(IEnumerable<string> flags) => flags.Select(f => f.Split('(')[0]);

    [Fact]
    public void Finger_map_is_qwerty()
    {
        Assert.Equal((0, 0), MotionScore.FingerOf("a"));
        Assert.Equal((0, 3), MotionScore.FingerOf("f"));
        Assert.Equal((0, 3), MotionScore.FingerOf("G"));
        Assert.Equal((1, 3), MotionScore.FingerOf("j"));
        Assert.Equal((1, 0), MotionScore.FingerOf("p"));
        Assert.Equal((2, 4), MotionScore.FingerOf(" "));
        Assert.Equal((2, 4), MotionScore.FingerOf("Space"));
        Assert.Null(MotionScore.FingerOf("Enter"));
    }

    [Theory]
    [InlineData("glide", "straight")]
    [InlineData("typed", "robot")]
    [InlineData("wheel", "metronome")]
    [InlineData("hover", "none")]
    [InlineData("drag", "teleport")]
    public void Bad_synthetic_trace_scores_at_most_half(string kind, string name)
    {
        var r = Score(kind, name);
        Assert.True(r.Score <= 0.5, $"{kind}/{name} {r.Score} {string.Join(",", r.Flags)}");
        Assert.NotEmpty(r.Flags);
    }

    [Theory]
    [InlineData("glide", "planned")]
    [InlineData("typed", "human")]
    [InlineData("wheel", "notch-bursts")]
    [InlineData("wheel", "trackpad")]
    [InlineData("hover", "dwell")]
    [InlineData("drag", "hesitant")]
    [InlineData("ambient", "planned")]
    public void Human_shaped_trace_scores_at_least_0_7(string kind, string name)
    {
        var r = Score(kind, name);
        Assert.True(r.Score >= 0.7, $"{kind}/{name} {r.Score} {string.Join(",", r.Flags)}");
    }

    [Fact]
    public void Every_shared_vector_scores_like_python()
    {
        foreach (var c in Vec.GetProperty("scores").EnumerateArray())
        {
            var r = MotionScore.ScoreActionJson(c.GetProperty("kind").GetString()!, c.GetProperty("samples"));
            var want = c.GetProperty("flags").EnumerateArray().Select(f => f.GetString()!);
            Assert.True(Math.Abs(r.Score - c.GetProperty("score").GetDouble()) < 1e-9,
                $"{c.GetProperty("kind")}/{c.GetProperty("name")}: {r.Score} vs {c.GetProperty("score")} ({string.Join(",", r.Flags)})");
            Assert.Equal(Names(want), Names(r.Flags));
        }
    }

    [Fact]
    public void Unknown_kind_throws() => Assert.Throws<ArgumentException>(() => MotionScore.ScoreAction("teleport", new List<MotionSample>()));

    private static List<string> LinesWith(string file, int plannedN) =>
        File.Exists(file)
            ? File.ReadAllLines(file).Where(l => l.Length > 0)
                .Where(l => { using var d = JsonDocument.Parse(l); return d.RootElement.TryGetProperty("meta", out var m) && m.TryGetProperty("planned_n", out var p) && p.GetInt32() == plannedN; })
                .ToList()
            : new List<string>();

    [Fact]
    public async Task Walk_records_one_line_with_actual_dispatch_times_and_warns_when_bad()
    {
        var dir = Directory.CreateTempSubdirectory("cc-score-").FullName;
        var file = Path.Combine(dir, "score.jsonl");
        var saved = Console.Error;
        var sw = new StringWriter();
        try
        {
            using var _ = new Sandbox().Env(HumanizeTrace.ScoreEnv, "1").Env(HumanizeTrace.ScoreFileEnv, file).Env("CLEARCOTE_NO_WARN", null);
            double clock = 0;
            Func<double> now = () => clock;
            Func<double, Task> sleep = ms => { clock += Math.Max(0, ms); return Task.CompletedTask; };

            var steps = Motion.PlanMove(new Point(100, 100), new Point(800, 500), Motion.MakePersona("ha-1"), targetW: 40, rng: Motion.Mulberry32(5));
            await Pacing.WalkAsync(steps, (x, y) => Task.CompletedTask, now, sleep);
            var good = LinesWith(file, steps.Count);
            Assert.Single(good);
            using (var d = JsonDocument.Parse(good[0]))
            {
                Assert.Equal("glide", d.RootElement.GetProperty("kind").GetString());
                Assert.Equal(steps.Count, d.RootElement.GetProperty("n").GetInt32());
            }

            // a 25 ms dispatch: the line holds what went out, not the plan
            int sent = await Pacing.WalkAsync(steps, (x, y) => { clock += 25; return Task.CompletedTask; }, now, sleep);
            var slow = LinesWith(file, steps.Count);
            Assert.Equal(2, slow.Count);
            using (var d = JsonDocument.Parse(slow[1])) Assert.Equal(sent, d.RootElement.GetProperty("n").GetInt32());
            Assert.True(sent < steps.Count);

            LaunchWarnings.ForgetSaid();
            Console.SetError(sw);
            var bad = Enumerable.Range(0, 61).Select(i => new Step(100 + i * 5, 100, 8)).ToList();   // 61: a planned_n no planner emits here
            await Pacing.WalkAsync(bad, (x, y) => Task.CompletedTask, now, sleep);
            var bl = LinesWith(file, 61);
            Assert.Single(bl);
            using (var d = JsonDocument.Parse(bl[0])) Assert.True(d.RootElement.GetProperty("score").GetDouble() < 0.5);
            Assert.Contains("clearcote: warning: humanize glide scored", sw.ToString());
            Assert.Contains("no-corrective-submovement", sw.ToString());
        }
        finally
        {
            Console.SetError(saved);
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public async Task Off_by_default_and_a_failing_tap_never_breaks_the_walk()
    {
        var dir = Directory.CreateTempSubdirectory("cc-score-").FullName;
        try
        {
            double clock = 0;
            Func<double> now = () => clock;
            Func<double, Task> sleep = ms => { clock += Math.Max(0, ms); return Task.CompletedTask; };
            var two = new List<Step> { new(1, 1, 8), new(50, 5, 8) };
            using (new Sandbox().Env(HumanizeTrace.ScoreEnv, "0"))
                Assert.Null(HumanizeTrace.Record("glide", new List<MotionSample> { new(1, 1, 0), new(2, 2, 8) }));
            using (new Sandbox().Env(HumanizeTrace.ScoreEnv, "1").Env(HumanizeTrace.ScoreFileEnv, Path.Combine(dir, "no-such-dir", "x.jsonl")))
                Assert.Equal(2, await Pacing.WalkAsync(two, (x, y) => Task.CompletedTask, now, sleep));
            Assert.Empty(Directory.GetFileSystemEntries(dir));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void Record_is_the_tap_for_the_typing_wheel_and_hover_loops()
    {
        var dir = Directory.CreateTempSubdirectory("cc-score-").FullName;
        var file = Path.Combine(dir, "score.jsonl");
        try
        {
            using var _ = new Sandbox().Env(HumanizeTrace.ScoreEnv, "1").Env(HumanizeTrace.ScoreFileEnv, file).Env("CLEARCOTE_NO_WARN", "1");
            var keys = Case("typed", "robot").GetProperty("samples").EnumerateArray()
                .Select(e => new KeySample(e.GetProperty("key").GetString()!, e.GetProperty("down").GetDouble(), e.GetProperty("up").GetDouble())).ToList();
            var r = HumanizeTrace.Record("typed", keys, new Dictionary<string, object?> { ["field"] = "#name" });
            Assert.NotNull(r);
            Assert.Equal("typed", r!.Kind);
            var line = File.ReadAllLines(file).Single(l => l.Contains("\"#name\""));
            using var d = JsonDocument.Parse(line);
            Assert.True(d.RootElement.GetProperty("score").GetDouble() < 0.5);
            Assert.Null(HumanizeTrace.Record("wheel", new List<WheelSample>()));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }
}
