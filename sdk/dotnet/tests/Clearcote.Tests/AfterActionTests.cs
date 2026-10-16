using System.Diagnostics;
using System.Reflection;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// After-action behaviour (SC 232 lane C) -- the .NET side of sdk/python/tests/test_humanize_after_action.py and
/// sdk/node/test/after-action.test.ts. Measured on r42 / SDK 0.51.0: a hover stopped dead on arrival (0 samples after
/// the glide's last one), the pointer never left a clicked element, .NET had no ambient motion at all (PlanAmbient
/// was not ported). Planners pinned to Python/Node by sdk/testdata/humanize_after_vectors.json; behaviour on a
/// DispatchProxy page (real time: the glides walk on the wall clock).
public class AfterActionTests
{
    private static readonly string[] Seeds = { "ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6" };
    private const double BX = 400, BY = 300, BW = 120, BH = 40;

    // ------------------------------------------------------------------------------ the fake page
    public class Cdp : DispatchProxy
    {
        public int TextLen { get; set; }
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            var name = (string)args![0]!;
            var expr = (args.Length > 1 ? args[1] as Dictionary<string, object> : null)?.GetValueOrDefault("expression") as string;
            string json = name switch
            {
                "Page.getFrameTree" => "{\"frameTree\":{\"frame\":{\"id\":\"MAIN\"}}}",
                "Page.createIsolatedWorld" => "{\"executionContextId\":5}",
                "DOM.getNodeForLocation" => "{\"backendNodeId\":7,\"frameId\":\"MAIN\"}",
                "DOM.resolveNode" => "{\"object\":{\"objectId\":\"o7\"}}",
                "Runtime.callFunctionOn" => "{\"result\":{\"value\":true}}",
                "Runtime.releaseObject" => "{}",
                "Runtime.evaluate" => Eval(expr ?? ""),
                _ => "{}",
            };
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());
        }

        private string Eval(string e)
        {
            string V(string v) => "{\"result\":{\"value\":" + v + "}}";
            if (e.StartsWith($"({IsolatedWorld.Viewport})", StringComparison.Ordinal)) return V("[1280,800]");
            if (e.StartsWith($"({IsolatedWorld.Platform})", StringComparison.Ordinal)) return V("\"Win32\"");
            if (e.StartsWith($"({IsolatedWorld.Scroll})", StringComparison.Ordinal)) return V("[0,0]");
            if (e.StartsWith($"({Attention.TextLength})", StringComparison.Ordinal)) return V(TextLen.ToString());
            return V("null");
        }
    }

    public class Mouse : DispatchProxy
    {
        public readonly Stopwatch Clock = Stopwatch.StartNew();
        public List<(string Op, double T, float X, float Y)> Log { get; } = new();
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            double t = Clock.Elapsed.TotalMilliseconds;
            switch (method?.Name)
            {
                case "MoveAsync": lock (Log) Log.Add(("move", t, (float)args![0]!, (float)args[1]!)); break;
                case "DownAsync": lock (Log) Log.Add(("down", t, 0, 0)); break;
                case "UpAsync": lock (Log) Log.Add(("up", t, 0, 0)); break;
                case "WheelAsync": lock (Log) Log.Add(("wheel", t, (float)args![0]!, (float)args[1]!)); break;
            }
            return Task.CompletedTask;
        }
    }

    public class Loc : DispatchProxy
    {
        public IPage? Page { get; set; }
        public List<string> Native { get; } = new();
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            switch (method?.Name)
            {
                case "get_Page": return Page;
                case "get_First": return this;
                case "IsVisibleAsync": return Task.FromResult(true);
                case "IsEnabledAsync": return Task.FromResult(true);
                case "CountAsync": return Task.FromResult(1);
                case "BoundingBoxAsync":
                    return Task.FromResult<LocatorBoundingBoxResult?>(new LocatorBoundingBoxResult { X = (float)BX, Y = (float)BY, Width = (float)BW, Height = (float)BH });
                case "ClickAsync": Native.Add("click"); return Task.CompletedTask;
                case "HoverAsync": Native.Add("hover"); return Task.CompletedTask;
            }
            if (method?.ReturnType == typeof(Task)) return Task.CompletedTask;
            return null;
        }
    }

    public class Page : HumanizeShiftTests.Recorder
    {
        public object? Context { get; set; }
        public object? Loc { get; set; }
        public EventHandler<IPage>? Load;
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            switch (method?.Name)
            {
                case "get_Context": return Context;
                case "Locator": return Loc;
                case "add_Load": Load += (EventHandler<IPage>)args![0]!; return null;
                case "remove_Load": return null;
            }
            if (method?.Name.StartsWith("Evaluate", StringComparison.Ordinal) == true)
                throw new InvalidOperationException("humanize must not evaluate in the page's world");
            return base.Invoke(method, args);
        }
    }

    private sealed record Fake(IPage Page, ILocator Loc, Mouse Mouse, Cdp Cdp, Page Proxy)
    {
        public List<(string Op, double T, float X, float Y)> Log => Mouse.Log;
        public double Now => Mouse.Clock.Elapsed.TotalMilliseconds;
        public void FireLoad() => Proxy.Load?.Invoke(Page, Page);
        public List<(string Op, double T, float X, float Y)> Moves(double since = double.NegativeInfinity)
        {
            lock (Log) return Log.Where(e => e.Op == "move" && e.T >= since).ToList();
        }
    }

    private static Fake Make(string seed, bool attach = true)
    {
        var cdp = DispatchProxy.Create<ICDPSession, Cdp>();
        var ctx = DispatchProxy.Create<IBrowserContext, IsolatedWorldTests.FakeContext>();
        var mouse = DispatchProxy.Create<IMouse, Mouse>();
        var kb = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, Page>();
        var loc = DispatchProxy.Create<ILocator, Loc>();
        ((IsolatedWorldTests.FakeContext)(object)ctx).Cdp = cdp;
        var pp = (Page)(object)page;
        pp.Context = ctx; pp.Mouse = mouse; pp.Keyboard = kb; pp.Loc = loc;
        ((Loc)(object)loc).Page = page;
        if (attach) Humanize.Attach(page, seed);
        return new Fake(page, loc, (Mouse)(object)mouse, (Cdp)(object)cdp, pp);
    }

    private static void AttentionOn(IPage page) => Humanize.AttentionFlag.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<bool>(true));
    private static List<object[]> Trace(IPage page) => Humanize.AfterTrace(page).ToList();

    private static ((string Op, double T, float X, float Y) At, List<(string Op, double T, float X, float Y)> Tail) AfterArrival(Fake f, double t0)
    {
        var mv = f.Moves(t0);
        int arrive = -1;
        for (int i = 0; i < mv.Count; i++) if (mv[i].X == Math.Round(mv[i].X) && mv[i].Y == Math.Round(mv[i].Y)) arrive = i;
        return (mv[arrive], mv.Skip(arrive + 1).ToList());
    }

    // ------------------------------------------------------------------------------- hover dwell
    [Fact]
    public async Task Hover_dwells_on_arrival_inside_the_box_per_seed()
    {
        var planned = new List<double>();
        foreach (var seed in Seeds)
        {
            var f = Make(seed);
            double t0 = f.Now;
            await f.Loc.HumanHoverAsync();
            double end = f.Now;
            var (at, rest) = AfterArrival(f, t0);
            Assert.True(rest.Count >= 3, $"{seed}: {rest.Count} samples after arrival");
            foreach (var e in rest)
            {
                Assert.True(Math.Sqrt((e.X - at.X) * (e.X - at.X) + (e.Y - at.Y) * (e.Y - at.Y)) <= 3.0 + 1e-3, $"{seed}: drifted off");
                Assert.True(e.X > BX && e.X < BX + BW && e.Y > BY && e.Y < BY + BH, $"{seed}: left the box");
            }
            Assert.True(end - at.T >= 140, $"{seed}: dwelt {end - at.T:F0} ms");
            var d = Trace(f.Page).Last(e => (string)e[0] == "dwell");
            double ms = (double)d[1];
            Assert.InRange(ms, 150, 800);
            planned.Add(ms);
        }
        Assert.True(planned.Select(v => Math.Round(v)).Distinct().Count() >= 4, string.Join(",", planned));
    }

    [Fact]
    public async Task Hover_dwell_zero_disables_and_a_value_overrides()
    {
        var f = Make("ha-1");
        double t0 = f.Now;
        await f.Loc.HumanHoverAsync(new HumanClickOptions { Dwell = 0 });
        Assert.Empty(AfterArrival(f, t0).Tail);
        t0 = f.Now;
        await f.Loc.HumanHoverAsync(new HumanClickOptions { Dwell = 400 });
        var (at, rest) = AfterArrival(f, t0);
        Assert.True(rest.Count >= 3);
        Assert.True(f.Now - at.T >= 370, $"dwelt {f.Now - at.T:F0} ms");
    }

    // ---------------------------------------------------------------------------------- move-off
    [Fact]
    public void Move_off_is_scheduled_in_60_to_85_percent_of_40_clicks_per_seed()
    {
        var schedule = typeof(Humanize).GetMethod("ScheduleMoveOff", BindingFlags.NonPublic | BindingFlags.Static)!;
        var stateFor = typeof(Humanize).GetMethod("StateFor", BindingFlags.NonPublic | BindingFlags.Static)!;
        foreach (var seed in Seeds)
        {
            var f = Make(seed);
            var p = Humanize.PersonaFor(f.Page);
            var st = stateFor.Invoke(null, new object[] { f.Page })!;
            int hits = 0;
            for (int i = 0; i < 40; i++)
            {
                schedule.Invoke(null, new[] { f.Page, p, st });
                var af = typeof(Humanize).GetMethod("AfterOf", BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, new object[] { f.Page })!;
                if (af.GetType().GetField("MoveOff")!.GetValue(af) is not null) hits++;
            }
            Assert.InRange(hits / 40.0, 0.60, 0.85);
        }
    }

    [Fact]
    public async Task Move_off_after_a_click_200_to_700_ms_later_40_to_200_px_downward()
    {
        var f = Make("ha-2");
        int off = 0;
        for (int i = 0; i < 10; i++)
        {
            await f.Loc.HumanClickAsync();
            double tUp;
            lock (f.Log) tUp = f.Log.Last(e => e.Op == "up").T;
            var from = f.Moves().Last();
            double t = f.Now;
            bool moved = await f.Page.HumanSettleAsync();
            var mv = f.Moves(t);
            Assert.Equal(moved, mv.Count > 0);
            if (mv.Count > 0)
            {
                off++;
                double lag = mv[0].T - tUp;
                Assert.True(lag >= 190 && lag <= 760, $"move-off {lag:F0} ms after the release");
                var last = mv[^1];
                double d = Math.Sqrt((last.X - from.X) * (last.X - from.X) + (last.Y - from.Y) * (last.Y - from.Y));
                Assert.InRange(d, 12, 200.5);
                Assert.True(last.Y > from.Y, "downward");
            }
        }
        Assert.InRange(off, 3, 10);
    }

    [Fact]
    public async Task Move_off_is_dropped_when_the_next_action_comes_first()
    {
        var f = Make("ha-1");
        for (int i = 0; i < 4; i++)
        {
            await f.Loc.HumanClickAsync();
            await f.Loc.HumanClickAsync();   // at once: the glide to the next target is the move-off
        }
        Assert.DoesNotContain(Trace(f.Page), e => (string)e[0] == "move_off");
    }

    // ------------------------------------------------------------------------- never on trial
    [Fact]
    public async Task A_trial_moves_nothing_and_runs_nothing_after_action()
    {
        var f = Make("ha-1");
        AttentionOn(f.Page);
        f.FireLoad();
        await f.Loc.HumanClickAsync(new LocatorClickOptions { Trial = true });
        await f.Loc.HumanHoverAsync(new HumanClickOptions { Trial = true });
        Assert.Empty(f.Moves());
        Assert.Empty(Trace(f.Page));
    }

    // --------------------------------------------------------------------------------- attention
    private static async Task<(Fake F, double Ms)> FirstPress(string seed, int textLen)
    {
        var f = Make(seed);
        AttentionOn(f.Page);
        f.Cdp.TextLen = textLen;
        f.FireLoad();
        double t = f.Now;
        await f.Loc.HumanClickAsync();
        double down;
        lock (f.Log) down = f.Log.First(e => e.Op == "down").T;
        return (f, down - t);
    }

    [Fact]
    public async Task Attention_read_pause_scales_with_text()
    {
        foreach (var seed in new[] { "ha-1", "ha-4" })
        {
            var s = await FirstPress(seed, 120);
            var l = await FirstPress(seed, 20000);
            Assert.True(s.Ms >= 790, $"{seed}: short page {s.Ms:F0} ms");
            Assert.True(l.Ms - s.Ms >= 1400, $"{seed}: {s.Ms:F0} vs {l.Ms:F0} ms");
            Assert.Contains(Trace(l.F.Page), e => (string)e[0] == "drift");
        }
    }

    [Fact]
    public async Task Attention_is_off_by_default_and_idle_drifts_follow_a_long_gap()
    {
        var f = Make("ha-4");
        f.Cdp.TextLen = 20000;
        f.FireLoad();
        await f.Loc.HumanClickAsync();
        Assert.DoesNotContain(Trace(f.Page), e => (string)e[0] is "read" or "drift");
        var g = Make("ha-4");
        AttentionOn(g.Page);
        await g.Loc.HumanClickAsync();
        await Task.Delay(1700);
        await g.Loc.HumanClickAsync();
        var dr = Trace(g.Page).Where(e => (string)e[0] == "drift").ToList();
        Assert.InRange(dr.Count, 1, 2);
        Assert.All(dr, e => Assert.InRange((double)e[1], 300, 900));
    }

    [Fact]
    public async Task Ambient_load_mode_runs_a_burst_after_load_and_idle_mode_is_attention()
    {
        var f = Make("ha-1");
        Humanize.AmbientModes.AddOrUpdate(f.Page, "load");
        f.FireLoad();
        await Task.Delay(1500);
        Assert.True(f.Moves().Count > 20, $"{f.Moves().Count} moves after load");
        var g = Make("ha-1");
        Humanize.AmbientModes.AddOrUpdate(g.Page, "idle");
        g.FireLoad();
        await g.Loc.HumanClickAsync();
        Assert.Contains(Trace(g.Page), e => (string)e[0] == "read");
    }

    [Fact]
    public async Task HumanAmbientMotionAsync_lasts_about_ms_and_stays_in_the_viewport()
    {
        var f = Make("ha-3");
        var sw = Stopwatch.StartNew();
        await f.Page.HumanAmbientMotionAsync(800);
        double took = sw.Elapsed.TotalMilliseconds;
        var mv = f.Moves();
        Assert.True(mv.Count > 40, $"{mv.Count} moves");
        Assert.InRange(took, 600, 1800);
        Assert.All(mv, e => Assert.True(e.X >= 0 && e.X <= 1280 && e.Y >= 0 && e.Y <= 800));
    }

    [Fact]
    public async Task Seed_fixes_traits_only_per_action_draws_are_unseeded()
    {
        Assert.Equal(Motion.AfterTraitsOf(Motion.MakePersona("same-1")), Motion.AfterTraitsOf(Motion.MakePersona("same-1")));
        var seqs = new List<List<double>>();
        for (int k = 0; k < 2; k++)
        {
            var f = Make("same-1");
            for (int i = 0; i < 4; i++) await f.Loc.HumanHoverAsync();
            seqs.Add(Trace(f.Page).Where(e => (string)e[0] == "dwell").Select(e => (double)e[1]).ToList());
        }
        Assert.Equal(4, seqs[0].Count);
        Assert.NotEqual(seqs[0], seqs[1]);
    }

    // ---------------------------------------------------------------------------- shared vectors
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_after_vectors.json"))).RootElement.Clone();

    private static void Near(double a, double b, string what) => Assert.True(Math.Abs(a - b) < 1e-6, $"{what}: {a} vs {b}");

    private static void Pick(List<Step> steps, JsonElement c, string label)
    {
        Assert.True(steps.Count == c.GetProperty("n").GetInt32(), $"{label}: n {steps.Count}");
        Near(steps.Sum(s => s.SleepMs), c.GetProperty("total_ms").GetDouble(), label + " total");
        foreach (var s in c.GetProperty("samples").EnumerateArray())
        {
            int i = s[0].GetInt32();
            Near(steps[i].X, s[1].GetDouble(), $"{label} x[{i}]");
            Near(steps[i].Y, s[2].GetDouble(), $"{label} y[{i}]");
            Near(steps[i].SleepMs, s[3].GetDouble(), $"{label} sleep[{i}]");
        }
    }

    [Fact]
    public void Shared_vectors_after_traits()
    {
        foreach (var t in Vec.GetProperty("after_traits").EnumerateArray())
        {
            var a = Motion.AfterTraitsOf(Motion.MakePersona(t.GetProperty("persona").GetString()));
            Near(a.DwellMs, t.GetProperty("dwell_ms").GetDouble(), "dwell");
            Near(a.MoveOffRate, t.GetProperty("move_off_rate").GetDouble(), "rate");
            Near(a.MoveOffDelayMs, t.GetProperty("move_off_delay_ms").GetDouble(), "delay");
            Near(a.ReadTempo, t.GetProperty("read_tempo").GetDouble(), "tempo");
        }
    }

    [Fact]
    public void Shared_vectors_plan_ambient()
    {
        foreach (var c in Vec.GetProperty("ambient").EnumerateArray())
        {
            var f = c.GetProperty("from"); var vp = c.GetProperty("viewport");
            var steps = Motion.PlanAmbient(new Point(f[0].GetDouble(), f[1].GetDouble()), vp[0].GetDouble(), vp[1].GetDouble(),
                Motion.MakePersona(c.GetProperty("persona").GetString()), c.GetProperty("ms").GetDouble(),
                Motion.Mulberry32(c.GetProperty("rng").GetUInt32()));
            Pick(steps, c, $"ambient {c.GetProperty("persona").GetString()}");
        }
    }

    [Fact]
    public void Shared_vectors_plan_dwell()
    {
        foreach (var c in Vec.GetProperty("dwell").EnumerateArray())
        {
            var at = c.GetProperty("at");
            Box? box = c.GetProperty("box").ValueKind == JsonValueKind.Null ? null
                : new Box(c.GetProperty("box")[0].GetDouble(), c.GetProperty("box")[1].GetDouble(), c.GetProperty("box")[2].GetDouble(), c.GetProperty("box")[3].GetDouble());
            var steps = Motion.PlanDwell(new Point(at[0].GetDouble(), at[1].GetDouble()), box,
                Motion.MakePersona(c.GetProperty("persona").GetString()), c.GetProperty("ms").GetDouble(),
                Motion.Mulberry32(c.GetProperty("rng").GetUInt32()));
            Pick(steps, c, $"dwell {c.GetProperty("persona").GetString()}");
        }
    }

    [Fact]
    public void Shared_vectors_after_action_stream()
    {
        foreach (var c in Vec.GetProperty("streams").EnumerateArray())
        {
            string id = c.GetProperty("persona").GetString()!;
            var p = Motion.MakePersona(id);
            var r = Motion.AfterRng(p);
            Near(Motion.DwellMs(p, r), c.GetProperty("dwell_ms").GetDouble(), id + " dwell");
            int hits = 0, k = 0;
            var rolls = c.GetProperty("rolls"); var delays = c.GetProperty("delays"); var targets = c.GetProperty("targets");
            for (int i = 0; i < 40; i++)
            {
                bool hit = Motion.MoveOffRoll(p, r, hits, i);
                Assert.True((hit ? 1 : 0) == rolls[i].GetInt32(), $"{id} roll {i}");
                if (!hit) continue;
                hits++;
                Near(Motion.MoveOffDelayMs(p, r), delays[k].GetDouble(), $"{id} delay {k}");
                var t = Motion.PlanMoveOff(new Point(200 + (i * 137) % 800, 150 + (i * 89) % 600), 1280, 800, p, r);
                Near(t.X, targets[k][0].GetDouble(), $"{id} target x {k}");
                Near(t.Y, targets[k][1].GetDouble(), $"{id} target y {k}");
                k++;
            }
            var reads = c.GetProperty("reads");
            double[] lens = { 0, 120, 3000, 20000, 50000 };
            for (int i = 0; i < lens.Length; i++) Near(Attention.ReadPauseMs(lens[i], p, r), reads[i].GetDouble(), $"{id} read {i}");
            foreach (var want in c.GetProperty("notches").EnumerateArray()) Assert.Equal(want.GetInt32(), Attention.ExploreNotches(r));
            double[] gaps = { 1.0, 1.6, 5.0 };
            var idle = c.GetProperty("idle");
            for (int i = 0; i < gaps.Length; i++)
            {
                var got = Attention.IdleDrifts(gaps[i], r);
                Assert.Equal(idle[i].GetArrayLength(), got.Count);
                for (int j = 0; j < got.Count; j++) Near(got[j], idle[i][j].GetDouble(), $"{id} idle {i}.{j}");
            }
        }
    }
}
