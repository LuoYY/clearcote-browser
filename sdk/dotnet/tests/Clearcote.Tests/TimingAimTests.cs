using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Lane B (SC 232): per-persona timing and persona-seeded aim -- a port of
/// sdk/python/tests/test_humanize_timing_aim.py (pure model) and node/test/humanize-timing-aim.test.ts. The three
/// SDKs are pinned to the same values by sdk/testdata/humanize_timing_vectors.json.
/// </summary>
public class TimingAimTests
{
    private static readonly string[] Ids = { "ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6" };
    private static readonly Box B = new(100, 100, 160, 40);
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_timing_vectors.json"))).RootElement.Clone();

    private static double Median(IEnumerable<double> xs)
    {
        var s = xs.OrderBy(v => v).ToArray();
        int m = s.Length / 2;
        return s.Length % 2 == 1 ? s[m] : (s[m - 1] + s[m]) / 2;
    }

    private static bool Close(double a, double b, double tol = 1e-9) => Math.Abs(a - b) <= tol;

    [Fact]
    public void Existing_persona_fields_are_unchanged()
    {
        foreach (var r in Vec.GetProperty("persona_core").EnumerateArray())
        {
            var p = Motion.MakePersona(r.GetProperty("persona").GetString());
            var got = new double[] { p.Seed, p.DeviceHz, p.TremorHz, p.TremorAmp, p.DriftAmp, p.DriftTau, p.Jitter, p.FittsA,
                p.FittsB, p.PrimaryFrac, p.Overshoot, p.MaxCorrections, p.ApproachBias };
            var want = r.GetProperty("core").EnumerateArray().Select(e => e.GetDouble()).ToArray();
            Assert.Equal(want, got);
        }
    }

    public static IEnumerable<object[]> TimingCases() => new[]
    {
        new object[] { "click hold", 55.0, 230.0 },
        new object[] { "key dwell", 45.0, 120.0 },
        new object[] { "grab", 130.0, 360.0 },
        new object[] { "release", 90.0, 230.0 },
    };

    private static double Draw(string name, Persona p, Rng r) => name switch
    {
        "click hold" => Motion.ClickHold(p, r),
        "key dwell" => Motion.KeyDwell(p, r),
        "grab" => Motion.DragDwell(p, r).GrabMs,
        _ => Motion.DragDwell(p, r).ReleaseMs,
    };

    [Theory]
    [MemberData(nameof(TimingCases))]
    public void Timing_medians_differ_across_seeds_inside_the_human_band(string name, double lo, double hi)
    {
        var med = Ids.Select(id =>
        {
            var p = Motion.MakePersona(id);
            var rng = Motion.Mulberry32(5);   // the same sample stream for every persona: only the persona differs
            return Median(Enumerable.Range(0, 400).Select(_ => Draw(name, p, rng)).ToList());
        }).ToList();
        Assert.True(med.Select(m => Math.Round(m)).Distinct().Count() >= 4, $"{name}: {string.Join(",", med)}");
        foreach (var m in med) Assert.InRange(m, lo, hi);
        var pb = Motion.MakePersona("band");
        var rb = Motion.Mulberry32(1);
        for (int i = 0; i < 2000; i++) Assert.InRange(Draw(name, pb, rb), lo, hi);
    }

    [Fact]
    public void Typo_rate_is_per_persona_and_the_env_var_still_overrides()
    {
        var saved = Environment.GetEnvironmentVariable(Humanize.TypoRateEnv);
        try
        {
            Environment.SetEnvironmentVariable(Humanize.TypoRateEnv, null);
            var rates = Ids.Select(id => Humanize.TypoRate(Motion.MakePersona(id))).ToList();
            Assert.True(rates.Select(r => Math.Round(r, 4)).Distinct().Count() >= 4);
            foreach (var r in rates) Assert.InRange(r, 0.005, 0.04);
            Assert.Equal(Forms.DefaultTypoRate, Humanize.TypoRate());
            Environment.SetEnvironmentVariable(Humanize.TypoRateEnv, "0.3");
            Assert.Equal(0.3, Humanize.TypoRate(Motion.MakePersona("ha-1")), 9);
        }
        finally
        {
            Environment.SetEnvironmentVariable(Humanize.TypoRateEnv, saved);
        }
    }

    private static List<Point> Landings(string id, int n = 200)
    {
        var p = Motion.MakePersona(id);   // a fresh persona: its aim stream starts over
        return Enumerable.Range(0, n).Select(_ => Motion.ClickPoint(B, new Point(0, 0), p)).ToList();
    }

    [Fact]
    public void ClickPoint_is_reproducible_per_seed_and_differs_across_seeds()
    {
        Assert.Equal(Landings("ha-1"), Landings("ha-1"));
        foreach (var id in Ids.Skip(1)) Assert.NotEqual(Landings("ha-1"), Landings(id));
    }

    [Fact]
    public void ClickPoint_dispersion_is_a_persona_trait_of_a_tenth_to_a_fifth_of_the_box()
    {
        var sds = Ids.Select(id =>
        {
            var xs = Landings(id, 600).Select(pt => pt.X).ToList();
            double mu = xs.Average();
            return Math.Sqrt(xs.Sum(x => (x - mu) * (x - mu)) / xs.Count) / B.Width;
        }).ToList();
        foreach (var s in sds) Assert.InRange(s, 0.08, 0.21);
        Assert.True(sds.Max() - sds.Min() > 0.02);
    }

    [Fact]
    public void ClickPoint_works_without_a_persona()
    {
        for (int i = 0; i < 50; i++)
        {
            var pt = Motion.ClickPoint(B, new Point(0, 0), null);
            Assert.InRange(pt.X, B.X + 2, B.X + B.Width - 2);
            Assert.InRange(pt.Y, B.Y + 2, B.Y + B.Height - 2);
        }
    }

    [Fact]
    public void Hold_position_is_padding_box_relative()
    {
        var box = new Box(100, 50, 200, 40);
        Assert.Equal(new Point(112, 56), HoldPlan.HoldPoint(box, position: new Microsoft.Playwright.Position { X = 10, Y = 5 }, border: (2, 1)));
    }

    // ---- shared vectors ----------------------------------------------------------------------------------------

    [Fact]
    public void Shared_vectors_timing_and_aim_traits()
    {
        foreach (var t in Vec.GetProperty("timing_traits").EnumerateArray())
        {
            var tt = Motion.TimingTraitsOf(Motion.MakePersona(t.GetProperty("persona").GetString()));
            Assert.True(Close(tt.ClickHoldMs, t.GetProperty("click_hold_ms").GetDouble(), 1e-12));
            Assert.True(Close(tt.KeyDwellMs, t.GetProperty("key_dwell_ms").GetDouble(), 1e-12));
            Assert.True(Close(tt.GrabMs, t.GetProperty("grab_ms").GetDouble(), 1e-12));
            Assert.True(Close(tt.ReleaseMs, t.GetProperty("release_ms").GetDouble(), 1e-12));
            Assert.True(Close(tt.DblclickGapMs, t.GetProperty("dblclick_gap_ms").GetDouble(), 1e-12));
            Assert.True(Close(tt.PauseScale, t.GetProperty("pause_scale").GetDouble(), 1e-12));
            Assert.True(Close(tt.TypoRate, t.GetProperty("typo_rate").GetDouble(), 1e-12));
        }
        foreach (var t in Vec.GetProperty("aim_traits").EnumerateArray())
        {
            var a = Motion.AimTraitsOf(Motion.MakePersona(t.GetProperty("persona").GetString()));
            Assert.True(Close(a.Spread, t.GetProperty("spread").GetDouble(), 1e-12));
            Assert.True(Close(a.Undershoot, t.GetProperty("undershoot").GetDouble(), 1e-12));
            Assert.True(Close(a.SideBias, t.GetProperty("side_bias").GetDouble(), 1e-12));
        }
    }

    [Fact]
    public void Shared_vectors_draws()
    {
        var bx = Vec.GetProperty("box");
        var box = new Box(bx.GetProperty("x").GetDouble(), bx.GetProperty("y").GetDouble(),
            bx.GetProperty("width").GetDouble(), bx.GetProperty("height").GetDouble());
        var from = new Point(Vec.GetProperty("from")[0].GetDouble(), Vec.GetProperty("from")[1].GetDouble());
        foreach (var d in Vec.GetProperty("draws").EnumerateArray())
        {
            string id = d.GetProperty("persona").GetString()!;
            var p = Motion.MakePersona(id);
            var r = Motion.Mulberry32(d.GetProperty("rng").GetUInt32());
            // the generator's order: drag_dwell, click_hold, key_dwell, dblclick_gap, pause, click_point
            var (grab, release) = Motion.DragDwell(p, r);
            double hold = Motion.ClickHold(p, r), dwell = Motion.KeyDwell(p, r), gap = Motion.DblclickGap(p, r);
            double pause = Motion.PauseMs(p, 220, 480, r);
            Assert.True(Close(grab, d.GetProperty("grab").GetDouble()), $"{id} grab {grab}");
            Assert.True(Close(release, d.GetProperty("release").GetDouble()), $"{id} release");
            Assert.True(Close(hold, d.GetProperty("click_hold").GetDouble()), $"{id} hold");
            Assert.True(Close(dwell, d.GetProperty("key_dwell").GetDouble()), $"{id} dwell");
            Assert.True(Close(gap, d.GetProperty("dblclick_gap").GetDouble()), $"{id} gap");
            Assert.True(Close(pause, d.GetProperty("pause_220_480").GetDouble()), $"{id} pause");
            var cp = Motion.ClickPoint(box, from, p, r);
            Assert.True(Close(cp.X, d.GetProperty("click_point_rng")[0].GetDouble())
                && Close(cp.Y, d.GetProperty("click_point_rng")[1].GetDouble()), $"{id} click point");
            var fresh = Motion.MakePersona(id);
            foreach (var w in d.GetProperty("click_point_stream").EnumerateArray())
            {
                var pt = Motion.ClickPoint(box, from, fresh);
                Assert.True(Close(pt.X, w[0].GetDouble()) && Close(pt.Y, w[1].GetDouble()), $"{id} stream {pt}");
            }
        }
    }
}
