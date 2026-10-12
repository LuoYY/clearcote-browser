using System.Diagnostics;
using System.Reflection;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// Wall-clock pacing of planned pointer paths (SC 210 lane P; mirrors the Python and Node tests).
// The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old loop awaited every move and
// THEN slept the planned gap, so a 27 ms move round trip (PRO r38 median) turned a ~1 s glide into
// 3.5-5.4 s (SC 209). The glide is driven through the real (private) GlideAsync with a fake mouse whose
// move costs 0 or 25 ms. .NET's planner draws from Random.Shared, so the planned duration is bounded by
// sampling the same planner for the same move; on 0.48.0 the 25 ms case runs ~3x past that bound.
public class HumanizePacingTests
{
    public class Mouse : DispatchProxy
    {
        public int LatencyMs { get; set; }
        public Stopwatch Clock { get; } = Stopwatch.StartNew();
        public List<(string Op, double T, float X, float Y)> Log { get; } = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            return method?.Name switch
            {
                "MoveAsync" => Ev("move", (float)args![0]!, (float)args[1]!),
                "DownAsync" => Ev("down", 0, 0),
                "UpAsync" => Ev("up", 0, 0),
                _ => Task.CompletedTask,
            };
        }

        private async Task Ev(string op, float x, float y)
        {
            if (LatencyMs > 0) await Task.Delay(LatencyMs);
            lock (Log) Log.Add((op, Clock.Elapsed.TotalMilliseconds, x, y));
        }
    }

    private static readonly MethodInfo Glide = typeof(Humanize).GetMethod("GlideAsync", BindingFlags.NonPublic | BindingFlags.Static)!;
    private static readonly Type StateType = typeof(Humanize).GetNestedType("PointerState", BindingFlags.NonPublic)!;

    private static object State(double x, double y)
    {
        var st = Activator.CreateInstance(StateType, nonPublic: true)!;
        StateType.GetField("X")!.SetValue(st, x);
        StateType.GetField("Y")!.SetValue(st, y);
        StateType.GetField("Known")!.SetValue(st, true);
        return st;
    }

    private static (double Lo, double Hi, double MaxGap) PlannedRange(Persona p, Point from, Point to, double w, bool settle)
    {
        double lo = double.MaxValue, hi = 0, gap = 0;
        for (int k = 0; k < 400; k++)
        {
            var plan = Motion.PlanMove(from, to, p, w, settle);
            double sum = plan.Sum(s => s.SleepMs);
            lo = Math.Min(lo, sum); hi = Math.Max(hi, sum);
            var prev = from;
            foreach (var s in plan)
            {
                gap = Math.Max(gap, Math.Sqrt((s.X - prev.X) * (s.X - prev.X) + (s.Y - prev.Y) * (s.Y - prev.Y)));
                prev = new Point(s.X, s.Y);
            }
        }
        return (lo, hi, gap);
    }

    [Theory]
    [InlineData(0, false)]
    [InlineData(25, false)]
    [InlineData(25, true)]    // the held-drag leg (settle) DragToAsync and range drags walk
    public async Task Glide_lasts_its_planned_duration(int latencyMs, bool settle)
    {
        var mouse = DispatchProxy.Create<IMouse, Mouse>();
        var m = (Mouse)(object)mouse;
        m.LatencyMs = latencyMs;
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        var persona = Motion.MakePersona("pace-net");
        var moves = new[] { (150.0, 150.0, 900.0, 600.0), (900.0, 600.0, 150.0, 150.0), (200.0, 300.0, 560.0, 310.0) };
        foreach (var (fx, fy, tx, ty) in moves)
        {
            var (lo, hi, ownGap) = PlannedRange(persona, new Point(fx, fy), new Point(tx, ty), 24, settle);
            int n0 = m.Log.Count;
            var sw = Stopwatch.StartNew();
            await (Task)Glide.Invoke(null, new object?[] { page, persona, State(fx, fy), tx, ty, 24.0, settle })!;
            double took = sw.Elapsed.TotalMilliseconds;
            Assert.True(took >= 0.8 * lo && took <= 1.2 * hi,
                $"glide took {took:F0} ms; the planner plans {lo:F0}-{hi:F0} ms for this move");
            var leg = m.Log.Skip(n0).ToList();
            Assert.All(leg, e => Assert.Equal("move", e.Op));
            Assert.Equal(((float)tx, (float)ty), (leg[^1].X, leg[^1].Y));
            // No hop longer than a fast hand covers between the two events (3 px/ms, >= 40 px or the plan's own).
            double first = Math.Sqrt((leg[0].X - fx) * (leg[0].X - fx) + (leg[0].Y - fy) * (leg[0].Y - fy));
            Assert.True(first <= Math.Max(40, ownGap) + 0.01, $"the first sample hopped {first:F1} px");
            for (int k = 1; k < leg.Count; k++)
            {
                double d = Math.Sqrt(Math.Pow(leg[k].X - leg[k - 1].X, 2) + Math.Pow(leg[k].Y - leg[k - 1].Y, 2));
                double cap = Math.Max(Math.Max(40, ownGap), Pacing.FastPxPerMs * (leg[k].T - leg[k - 1].T));
                Assert.True(d <= cap + 0.01, $"a skipped stretch hopped {d:F1} px in {leg[k].T - leg[k - 1].T:F0} ms");
            }
        }
    }

    private sealed class FakeClock
    {
        public double T;
        public List<(double X, double Y, double T)> Sent { get; } = new();
        public Func<double, double, Task> Move(double lat) => (x, y) => { T += lat; Sent.Add((x, y, T)); return Task.CompletedTask; };
        public Task Sleep(double ms) { T += Math.Max(0, ms); return Task.CompletedTask; }
    }

    [Fact]
    public async Task Pacer_never_drops_a_pinned_step_or_the_last()
    {
        var steps = Enumerable.Range(0, 200).Select(i => new Step(i, 0, 2)).ToList();
        var pins = new HashSet<int> { 37, 38, 120 };
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, c.Move(25), () => c.T, c.Sleep, pinned: pins.Contains);
        var xs = c.Sent.Select(s => s.X).ToList();
        Assert.True(xs.Count < 100);
        foreach (var k in new[] { 37, 38, 120, 199 }) Assert.Contains((double)k, xs);
        Assert.Equal(xs.OrderBy(v => v), xs);
    }

    [Fact]
    public async Task Pacer_on_time_dispatches_every_sample_at_its_planned_time()
    {
        var steps = Enumerable.Range(0, 50).Select(i => new Step(i * 3, 0, 7)).ToList();
        var c = new FakeClock();
        Assert.Equal(50, await Pacing.WalkAsync(steps, c.Move(0), () => c.T, c.Sleep));
        for (int k = 0; k < 50; k++) Assert.Equal(7.0 * k, c.Sent[k].T, 6);
        Assert.Equal(350.0, c.T, 6);
    }

    [Fact]
    public async Task Pacer_pauses_through_a_stall_instead_of_skipping_after_it()
    {
        // Measured on PRO r38 under load: one move answered after 523 ms, the pacer then skipped to catch
        // up and the fixture's iframe crossing showed the page a 110 px jump. Now the plan pauses.
        var steps = Enumerable.Range(0, 200).Select(i => new Step(i * 2, 0, 5)).ToList();
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, (x, y) => { c.T += c.Sent.Count == 10 ? 500 : 2; c.Sent.Add((x, y, c.T)); return Task.CompletedTask; },
            () => c.T, c.Sleep);
        Assert.True(c.Sent.Count >= 185, $"{c.Sent.Count} sent");
        Assert.True(Math.Abs(c.T - (1000 + 500 - Pacing.StallMs)) < 30, $"took {c.T}");
        Assert.Equal(398.0, c.Sent[^1].X);
    }

    [Fact]
    public async Task Pacer_dispatches_the_in_between_point_instead_of_a_long_jump()
    {
        var steps = Enumerable.Range(0, 40).Select(i => new Step(i * 30, 0, 1)).ToList();
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, c.Move(25), () => c.T, c.Sleep);
        double prev = 0, gap = 0;
        foreach (var s in c.Sent) { gap = Math.Max(gap, s.X - prev); prev = s.X; }
        // 30 px/ms is ten times a fast hand: every hop stays within what one covers in a 25 ms dispatch.
        Assert.True(gap <= Pacing.FastPxPerMs * 25 + 1e-6, $"hop {gap}");
        Assert.True(c.Sent.Count > 10);
        Assert.Equal(39 * 30.0, c.Sent[^1].X);
    }
}
