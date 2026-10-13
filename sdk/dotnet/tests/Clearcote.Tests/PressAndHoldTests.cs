using System.Diagnostics;
using System.Reflection;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// PressAndHoldAsync's plan (Hold.cs, a port of sdk/python/clearcote/_hold.py; mirrors
/// sdk/python/tests/test_press_and_hold.py): hold length, drift, release reaction, press point, and the
/// driver's release guarantees against a fake page (no browser).
public class PressAndHoldTests
{
    [Fact]
    public void Duration_is_taken_exactly()
    {
        Assert.Equal(1234.0, HoldPlan.HoldMs(1234, false));
        Assert.Equal(500.0, HoldPlan.HoldMs(500, true));
    }

    [Theory]
    [InlineData(0.0)]
    [InlineData(-5.0)]
    [InlineData(double.NaN)]
    [InlineData(double.PositiveInfinity)]
    public void Bad_duration_raises(double bad)
        => Assert.Throws<ArgumentException>(() => HoldPlan.HoldMs(bad, false));

    [Fact]
    public void Default_and_until_cap()
    {
        for (int i = 0; i < 200; i++)
        {
            var d = HoldPlan.HoldMs(null, false);
            Assert.InRange(d, 2800.0, 3800.0);
        }
        Assert.Equal(HoldPlan.UntilCapMs, HoldPlan.HoldMs(null, true));
        Assert.Equal(15000.0, HoldPlan.UntilCapMs);
        Assert.Equal(4000.0, HoldPlan.HoldMs(4000, true));
        Assert.Equal(2800.0, HoldPlan.HoldMs(null, false, () => 0.0));
    }

    [Fact]
    public void Tremor_stays_near_the_press_point_and_is_sparse()
    {
        var r = new Random(7);
        Rng rng = r.NextDouble;
        for (int k = 0; k < 50; k++)
        {
            var plan = HoldPlan.TremorPlan(10000, rng);
            Assert.NotEmpty(plan);   // a 10 s hold drifts at least once
            double prevT = 0;
            (int X, int Y) prev = (0, 0);
            foreach (var s in plan)
            {
                Assert.True(s.T > prevT && s.T < 10000 - 120);
                Assert.InRange(Math.Abs(s.Dx), 0, HoldPlan.TremorRadiusPx);
                Assert.InRange(Math.Abs(s.Dy), 0, HoldPlan.TremorRadiusPx);
                Assert.NotEqual(prev, (s.Dx, s.Dy));                                       // every planned move moves
                Assert.True(Math.Abs(s.Dx - prev.X) <= 1 && Math.Abs(s.Dy - prev.Y) <= 1);  // one pixel at a time
                prevT = s.T;
                prev = (s.Dx, s.Dy);
            }
            for (int i = 1; i < plan.Count; i++) Assert.InRange(plan[i].T - plan[i - 1].T, 250.0, 900.0);
            Assert.InRange(plan[0].T, 250.0, 650.0);
        }
    }

    [Fact]
    public void Short_hold_has_no_drift()
    {
        // the first drift comes 250-650 ms in, and none in the last 120 ms
        Assert.Empty(HoldPlan.TremorPlan(300, () => 0.0));
        Assert.Empty(HoldPlan.TremorPlan(700, () => 0.99));
    }

    [Fact]
    public void Tremor_matches_the_python_plan_for_a_fixed_stream()
    {
        // A fixed stream gives the plan Python's _hold.tremor_plan(2000, rng) gives for it (checked against it):
        // 0.0 draws a -1 step, 0.9 a +1 step, 0.5 a 575 ms gap.
        var seq = new[] { 0.5, 0.0, 0.9, 0.5, 0.0, 0.0, 0.5, 0.0, 0.0, 0.5 };
        int n = 0;
        Rng rng = () => seq[n++ % seq.Length];
        var plan = HoldPlan.TremorPlan(2000, rng);
        // t0 = 250 + 0.5*400 = 450; step (-1,+1); t += 250 + 0.5*650 = 575 -> 1025; step (-1,-1) -> (-2,0);
        // t -> 1600; step (-1,-1) -> x -3 bounces to -1, y -1 -> (-1,-1); t -> 2175 >= 1880: stop.
        Assert.Equal(new[] { new HoldPlan.Drift(450, -1, 1), new HoldPlan.Drift(1025, -2, 0), new HoldPlan.Drift(1600, -1, -1) },
            plan.ToArray());
    }

    [Fact]
    public void Reaction_range()
    {
        var r = new Random(3);
        for (int i = 0; i < 500; i++) Assert.InRange(HoldPlan.ReactionMs(r.NextDouble), 160.0, 350.0);
    }

    [Fact]
    public void Hold_point()
    {
        var box = new Box(100, 50, 200, 40);
        Assert.Equal(new Point(200, 70), HoldPlan.HoldPoint(box));
        Assert.Equal(new Point(110, 55), HoldPlan.HoldPoint(box, position: new Position { X = 10, Y = 5 }));
        var pt = HoldPlan.HoldPoint(box, new Point(0, 0), Motion.MakePersona("hold-1"));
        Assert.InRange(pt.X, 102, 298);
        Assert.InRange(pt.Y, 52, 88);
    }

    [Fact]
    public void Bad_button_raises()
    {
        Assert.Throws<ArgumentException>(() => HoldPlan.ButtonOf((MouseButton)42));
        Assert.Equal(MouseButton.Left, HoldPlan.ButtonOf(null));
        Assert.Equal(MouseButton.Middle, HoldPlan.ButtonOf(MouseButton.Middle));
    }

    // ---- the driver, against a fake page ------------------------------------------------------------------

    [Fact]
    public async Task Bad_options_raise_before_anything_moves()
    {
        var log = new List<string>();
        var loc = Fake.Locator(log);
        await Assert.ThrowsAsync<ArgumentException>(() => loc.PressAndHoldAsync(new PressAndHoldOptions { Duration = 0 }));
        await Assert.ThrowsAsync<ArgumentException>(() => loc.PressAndHoldAsync(new PressAndHoldOptions { Button = (MouseButton)9 }));
        Assert.Empty(log);
    }

    [Fact]
    public async Task Releases_when_until_throws()
    {
        var log = new List<string>();
        var loc = Fake.Locator(log);
        await Assert.ThrowsAsync<InvalidOperationException>(() => loc.PressAndHoldAsync(new PressAndHoldOptions
        {
            Duration = 2000, Until = () => throw new InvalidOperationException("until failed"),
        }));
        Assert.Equal("up Left", log[^1]);
    }

    [Fact]
    public async Task Until_releases_early_and_reports_it()
    {
        var log = new List<string>();
        var loc = Fake.Locator(log);
        int calls = 0;
        var sw = Stopwatch.StartNew();
        var ok = await loc.PressAndHoldAsync(new PressAndHoldOptions
        {
            Duration = 5000, Tremor = false, Until = () => Task.FromResult(++calls >= 3),
        });
        Assert.True(ok);
        Assert.InRange(sw.Elapsed.TotalSeconds, 0.3, 1.0);
        Assert.Equal(new[] { "move 60,25", "down Left", "up Left" }, log);
    }

    [Fact]
    public async Task Until_never_true_returns_false()
    {
        var log = new List<string>();
        var ok = await Fake.Locator(log).PressAndHoldAsync(new PressAndHoldOptions
        {
            Duration = 400, Tremor = false, Until = () => Task.FromResult(false),
        });
        Assert.False(ok);
        Assert.Equal("up Left", log[^1]);
    }

    [Fact]
    public async Task Position_button_and_tremor_moves_stay_within_two_px()
    {
        var log = new List<string>();
        var ok = await Fake.Locator(log).PressAndHoldAsync(new PressAndHoldOptions
        {
            Duration = 1500, Position = new Position { X = 20, Y = 15 }, Button = MouseButton.Right,
        });
        Assert.True(ok);
        Assert.Equal("move 30,25", log[0]);
        Assert.Equal("down Right", log[1]);
        Assert.Equal("up Right", log[^1]);
        var drift = log.Skip(2).Take(log.Count - 3).ToList();
        Assert.NotEmpty(drift);   // the first drift comes by 650 ms
        foreach (var m in drift)
        {
            var xy = m["move ".Length..].Split(',').Select(v => double.Parse(v, System.Globalization.CultureInfo.InvariantCulture)).ToArray();
            Assert.InRange(Math.Abs(xy[0] - 30), 0, 2);
            Assert.InRange(Math.Abs(xy[1] - 25), 0, 2);
        }
    }

    [Fact]
    public async Task Cancellation_mid_hold_still_releases()
    {
        var log = new List<string>();
        using var cts = new CancellationTokenSource(300);
        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => Fake.Locator(log).PressAndHoldAsync(new PressAndHoldOptions { Duration = 5000 }, cts.Token));
        Assert.Equal("down Left", log.First(l => !l.StartsWith("move")));
        Assert.Equal("up Left", log[^1]);
    }

    /// Interface fakes built with DispatchProxy: just the members PressAndHoldAsync touches.
    public class Fake : DispatchProxy
    {
        public List<string> Log = null!;
        public object? Page, Mouse;

        public static ILocator Locator(List<string> log)
        {
            var mouse = DispatchProxy.Create<IMouse, Fake>();
            ((Fake)(object)mouse).Log = log;
            var page = DispatchProxy.Create<IPage, Fake>();
            ((Fake)(object)page).Log = log;
            ((Fake)(object)page).Mouse = mouse;
            var loc = DispatchProxy.Create<ILocator, Fake>();
            ((Fake)(object)loc).Log = log;
            ((Fake)(object)loc).Page = page;
            return loc;
        }

        protected override object? Invoke(MethodInfo? m, object?[]? a)
        {
            switch (m!.Name)
            {
                case "get_Page": return Page;
                case "get_Mouse": return Mouse;
                case "ToString": return "Locator@fake";
                case "GetHashCode": return 1;
                case "WaitForAsync":
                case "ScrollIntoViewIfNeededAsync":
                    return Task.CompletedTask;
                case "BoundingBoxAsync":
                    return Task.FromResult<LocatorBoundingBoxResult?>(new LocatorBoundingBoxResult { X = 10, Y = 10, Width = 100, Height = 30 });
                case "MoveAsync":
                    lock (Log) Log.Add(FormattableString.Invariant($"move {a![0]},{a[1]}"));
                    return Task.CompletedTask;
                case "DownAsync":
                    lock (Log) Log.Add("down " + (((MouseDownOptions?)a![0])?.Button ?? MouseButton.Left));
                    return Task.CompletedTask;
                case "UpAsync":
                    lock (Log) Log.Add("up " + (((MouseUpOptions?)a![0])?.Button ?? MouseButton.Left));
                    return Task.CompletedTask;
            }
            throw new NotSupportedException(m.Name);
        }
    }
}
