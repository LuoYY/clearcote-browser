using System.Reflection;
using System.Text.Json;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// SC 232 integration (lane G's score hook in lanes C/D/E; mirrors python/tests/test_humanize_trace_taps.py): with
/// CLEARCOTE_HUMANIZE_SCORE=1 a typed run records one "typed" line of the characters' down/up, ambient motion an
/// "ambient" line, HumanWheelAsync one "wheel" line per call, and a held walk a "drag" line with the button on every
/// sample. Off by default: nothing is written.
/// </summary>
public class HumanizeTraceTapsTests
{
    private static (IPage Page, HumanizeShiftTests.Recorder Kb) FakePage()
    {
        var keyboard = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var mouse = DispatchProxy.Create<IMouse, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        var rec = (HumanizeShiftTests.Recorder)(object)page;
        rec.Keyboard = keyboard;
        rec.Mouse = mouse;
        Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        return (page, (HumanizeShiftTests.Recorder)(object)keyboard);
    }

    private static List<JsonElement> Lines(string file) => !File.Exists(file) ? new()
        : File.ReadAllLines(file).Where(l => l.Length > 0).Select(l => JsonDocument.Parse(l).RootElement.Clone()).ToList();

    private static string Kind(JsonElement l) => l.GetProperty("kind").GetString()!;

    private static async Task DriveAsync(IPage page)
    {
        await page.HumanTypeAsync("hello world");      // the ambient placement (a glide), then one typed run (lane D)
        await page.HumanAmbientMotionAsync(300);        // ambient (lane C's planner)
        await page.HumanWheelAsync(0, 300);             // one wheel call (lane E)
        double t = 0;                                   // a held walk, as HumanDragToAsync walks it (lane E)
        await Pacing.WalkAsync(Motion.PlanDrag(new Point(100, 100), new Point(400, 300), Motion.MakePersona("ha-1"), false, Motion.Mulberry32(3)),
            (_, _) => Task.CompletedTask, now: () => t, sleep: ms => { t += Math.Max(0, ms); return Task.CompletedTask; },
            kind: "drag", buttons: 1);
    }

    [Fact]
    public async Task Typed_ambient_wheel_and_drag_record_their_kind()
    {
        var dir = Directory.CreateTempSubdirectory("cc-taps-");
        var file = Path.Combine(dir.FullName, "score.jsonl");
        try
        {
            using var _ = new Sandbox().Env(HumanizeTrace.ScoreEnv, "1").Env(HumanizeTrace.ScoreFileEnv, file).Env("CLEARCOTE_NO_WARN", "1");
            var (page, _kb) = FakePage();
            await DriveAsync(page);
            var lines = Lines(file);
            var kinds = lines.Select(Kind).ToList();
            foreach (var k in new[] { "glide", "typed", "ambient", "wheel", "drag" }) Assert.Contains(k, kinds);
            var typed = Assert.Single(lines.Where(l => Kind(l) == "typed"));
            Assert.Equal(11, typed.GetProperty("n").GetInt32());
            Assert.Equal(11, typed.GetProperty("meta").GetProperty("n").GetInt32());
            Assert.False(typed.GetProperty("meta").GetProperty("typos").GetBoolean());
            var wheel = Assert.Single(lines.Where(l => Kind(l) == "wheel"));
            Assert.True(wheel.GetProperty("n").GetInt32() >= 1);
            Assert.Equal(300, wheel.GetProperty("meta").GetProperty("dy").GetDouble());
            var drag = Assert.Single(lines.Where(l => Kind(l) == "drag"));
            Assert.Equal(drag.GetProperty("n").GetDouble(), drag.GetProperty("features").GetProperty("held_n").GetDouble());
            foreach (var l in lines) Assert.InRange(l.GetProperty("score").GetDouble(), 0, 1);
        }
        finally { dir.Delete(true); }
    }

    [Fact]
    public async Task Off_by_default_nothing_is_written()
    {
        var dir = Directory.CreateTempSubdirectory("cc-taps-");
        var file = Path.Combine(dir.FullName, "score.jsonl");
        try
        {
            using var _ = new Sandbox().Env(HumanizeTrace.ScoreEnv, null).Env(HumanizeTrace.ScoreFileEnv, file);
            var (page, _kb) = FakePage();
            await DriveAsync(page);
            Assert.False(File.Exists(file));
        }
        finally { dir.Delete(true); }
    }
}
