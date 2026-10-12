using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Path SHAPE of the humanize planner (SC 210 lane S), measured on the planned points — a port of
/// sdk/python/tests/test_motion_shape.py. Metrics are the SC 209 Humanize Lab's (analyze.py
/// seg_metrics). Measured on r38 / SDK 0.48.0 the planned aimed moves were 0.99 straight and passed the
/// target in 1 of 36 approaches; a held drag wandered up to 47 px off its line. The three SDKs are
/// pinned to identical paths by sdk/testdata/humanize_motion_vectors.json.
/// </summary>
public class MotionShapeTests
{
    private static readonly string[] Ids = { "ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6" };
    private static readonly (double X, double Y, double W, double H)[] Targets =
    {
        (150, 142, 120, 44), (902, 613, 44, 26), (515, 377, 90, 34), (1040, 138, 160, 56), (198, 632, 36, 24), (675, 205, 70, 30),
    };

    private sealed record Shape(double Eff, double Over, double Dev, int Peaks, double TPeak);

    private static double Median(IEnumerable<double> xs)
    {
        var s = xs.OrderBy(x => x).ToArray();
        int m = s.Length / 2;
        return s.Length % 2 == 1 ? s[m] : (s[m - 1] + s[m]) / 2;
    }

    /// SC 209 seg_metrics on a planned path; quantize = what a DPR-1 page sees (whole pixels, no event
    /// while the pixel is unchanged).
    private static Shape PathShape(Point from, List<Step> steps, bool quantize = false)
    {
        var pts = new List<(double X, double Y, double T)> { (from.X, from.Y, 0) };
        double t = 0;
        foreach (var s in steps)
        {
            double x = s.X, y = s.Y;
            if (quantize)
            {
                x = Math.Floor(x); y = Math.Floor(y);
                if (x == pts[^1].X && y == pts[^1].Y) { t += s.SleepMs; continue; }
            }
            pts.Add((x, y, t));
            t += s.SleepMs;
        }
        var (x0, y0, _) = pts[0];
        var (x1, y1, t1) = pts[^1];
        double d = Math.Sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0));
        var cum = new List<double> { 0 };
        for (int i = 1; i < pts.Count; i++)
            cum.Add(cum[i - 1] + Math.Sqrt(Math.Pow(pts[i].X - pts[i - 1].X, 2) + Math.Pow(pts[i].Y - pts[i - 1].Y, 2)));
        double ux = (x1 - x0) / d, uy = (y1 - y0) / d;
        double over = Math.Max(0, pts.Max(p => (p.X - x0) * ux + (p.Y - y0) * uy) - d);
        double dev = pts.Max(p => Math.Abs((p.X - x0) * uy - (p.Y - y0) * ux));
        var ts = pts.Select(p => p.T).ToArray();
        double DistAt(double tt)
        {
            if (tt <= ts[0]) return 0;
            if (tt >= ts[^1]) return cum[^1];
            int lo = 0, hi = ts.Length - 1;
            while (hi - lo > 1) { int mid = (lo + hi) / 2; if (ts[mid] <= tt) lo = mid; else hi = mid; }
            if (ts[hi] == ts[lo]) return cum[hi];
            return cum[lo] + (tt - ts[lo]) / (ts[hi] - ts[lo]) * (cum[hi] - cum[lo]);
        }
        var prof = new List<double>();
        for (int k = 0; k <= (int)Math.Floor(t1 / 10); k++) prof.Add((DistAt(10 * k + 40) - DistAt(10 * k - 40)) / 80);
        double vmax = prof.Max();
        int peaks = 0;
        for (int k = 1; k < prof.Count - 1; k++)
        {
            if (!(prof[k] >= prof[k - 1] && prof[k] > prof[k + 1] && prof[k] > 0.2 * vmax)) continue;
            int lo = k; while (lo > 0 && prof[lo - 1] <= prof[k]) lo--;
            int hi = k; while (hi < prof.Count - 1 && prof[hi + 1] <= prof[k]) hi++;
            double leftMin = prof.Skip(lo).Take(k - lo + 1).Min(), rightMin = prof.Skip(k).Take(hi - k + 1).Min();
            if (prof[k] - Math.Max(leftMin, rightMin) >= 0.35 * vmax) peaks++;
        }
        return new Shape(d / cum[^1], over, dev, peaks, (prof.IndexOf(vmax) + 0.5) / prof.Count);
    }

    private static List<(Point From, List<Step> Steps, Point To)> AimedMoves(int rounds = 12)
    {
        var outList = new List<(Point, List<Step>, Point)>();
        for (int pi = 0; pi < Ids.Length; pi++)
        {
            var p = Motion.MakePersona(Ids[pi]);
            for (int r = 0; r < rounds; r++)
            {
                var rng = Motion.Mulberry32((uint)(1000 * pi + r));
                var cur = new Point(260, 165);
                foreach (var (cx, cy, w, h) in Targets)
                {
                    var to = new Point(cx, cy);
                    outList.Add((cur, Motion.PlanMove(cur, to, p, Math.Min(w, h), false, rng), to));
                    cur = to;
                }
            }
        }
        return outList;
    }

    private static List<(Point From, List<Step> Steps, Point To)> SliderDrags(int rounds = 16)
    {
        var outList = new List<(Point, List<Step>, Point)>();
        for (int pi = 0; pi < Ids.Length; pi++)
        {
            var p = Motion.MakePersona(Ids[pi]);
            for (int r = 0; r < rounds; r++)
            {
                var rng = Motion.Mulberry32((uint)(7000 + 100 * pi + r));
                double fx = 40 + rng() * 60;
                double fy = 300 + rng() * 40;
                double tx = fx + 300 + rng() * 100;
                double ty = fy + (rng() - 0.5) * 6;
                var from = new Point(fx, fy);
                var to = new Point(tx, ty);
                outList.Add((from, Motion.PlanMove(from, to, p, 24, true, rng), to));
            }
        }
        return outList;
    }

    [Fact]
    public void Aimed_moves_are_as_straight_as_real_reaches_not_ruler_lines()
    {
        foreach (var q in new[] { false, true })
        {
            var effs = AimedMoves().Select(m => PathShape(m.From, m.Steps, q).Eff).ToList();
            Assert.InRange(Median(effs), 0.85, 0.95);
            Assert.True(effs.Count(e => e > 0.995) / (double)effs.Count < 0.1);
        }
    }

    [Fact]
    public void Aimed_moves_overshoot_and_come_back_in_15_to_40_percent()
    {
        var moves = AimedMoves();
        double share = moves.Count(m => PathShape(m.From, m.Steps).Over > 2) / (double)moves.Count;
        Assert.InRange(share, 0.15, 0.40);
    }

    [Fact]
    public void Overshoot_rate_is_a_per_persona_trait()
    {
        foreach (var pid in Ids)
        {
            var p = Motion.MakePersona(pid);
            double rate = Motion.ShapeTraitsOf(p).OvershootRate;
            Assert.InRange(rate, 0.15, 0.38);
            var rng = Motion.Mulberry32(55);
            int n = 300, hits = 0;
            for (int i = 0; i < n; i++)
            {
                double fx = 100 + rng() * 200, fy = 100 + rng() * 200;
                double tx = fx + 150 + rng() * 500, ty = fy + (rng() - 0.5) * 400;
                var from = new Point(fx, fy);
                if (PathShape(from, Motion.PlanMove(from, new Point(tx, ty), p, 30, false, rng)).Over > 2) hits++;
            }
            Assert.True(Math.Abs(hits / (double)n - rate) < 0.08, $"{pid}: {hits / (double)n} vs {rate}");
        }
    }

    [Fact]
    public void Aimed_moves_land_exactly_on_the_target()
    {
        foreach (var m in AimedMoves(4))
            Assert.Equal((m.To.X, m.To.Y), (m.Steps[^1].X, m.Steps[^1].Y));
    }

    [Fact]
    public void Aimed_moves_reach_peak_speed_before_the_halfway_point()
    {
        var shapes = AimedMoves().Select(m => PathShape(m.From, m.Steps)).ToList();
        double tp = Median(shapes.Select(s => s.TPeak));
        Assert.True(tp >= 0.2 && tp < 0.45, $"t_peak {tp}");
        Assert.True(Median(shapes.Select(s => (double)s.Peaks)) <= 2);
    }

    [Fact]
    public void Aimed_moves_keep_the_Fitts_duration_cap()
    {
        var p = Motion.MakePersona("fitts-shape");
        for (uint k = 0; k < 40; k++)
        {
            var st = Motion.PlanMove(new Point(10, 10), new Point(1900, 1000), p, 8, false, Motion.Mulberry32(k));
            Assert.True(st.Sum(s => s.SleepMs) <= 1700 * 1.12 + 240);
        }
    }

    [Fact]
    public void Aimed_moves_are_deterministic_per_seed()
    {
        var a = Motion.PlanMove(new Point(0, 0), new Point(500, 300), Motion.MakePersona("det"), 20, false, Motion.Mulberry32(9));
        var b = Motion.PlanMove(new Point(0, 0), new Point(500, 300), Motion.MakePersona("det"), 20, false, Motion.Mulberry32(9));
        var c = Motion.PlanMove(new Point(0, 0), new Point(500, 300), Motion.MakePersona("det-2"), 20, false, Motion.Mulberry32(9));
        Assert.Equal(a, b);
        Assert.NotEqual(a, c);
    }

    [Fact]
    public void Slider_drags_stay_on_a_40px_track()
    {
        foreach (var q in new[] { false, true })
        {
            var shapes = SliderDrags().Select(m => PathShape(m.From, m.Steps, q)).ToList();
            var effs = shapes.Select(s => s.Eff).OrderBy(e => e).ToList();
            Assert.True(effs[effs.Count / 10] >= 0.93, $"p10 straightness {effs[effs.Count / 10]} (quantize {q})");
            Assert.True(shapes.Max(s => s.Dev) <= 8, $"max sideways {shapes.Max(s => s.Dev)}");
        }
    }

    [Fact]
    public void Slider_drags_take_half_a_second_to_two_seconds()
    {
        foreach (var m in SliderDrags())
            Assert.InRange(m.Steps.Sum(s => s.SleepMs), 500, 2000);
    }

    [Fact]
    public void Slider_drags_sometimes_overshoot_and_pull_back_then_land_exactly()
    {
        var drags = SliderDrags(40);
        double share = drags.Count(m => PathShape(m.From, m.Steps).Over > 2) / (double)drags.Count;
        Assert.InRange(share, 0.04, 0.35);
        foreach (var m in drags) Assert.Equal((m.To.X, m.To.Y), (m.Steps[^1].X, m.Steps[^1].Y));
    }

    [Fact]
    public void A_move_with_the_button_held_is_planned_as_a_drag()
    {
        var p = Motion.MakePersona("held");
        double worst = 0;
        for (uint k = 0; k < 40; k++)
        {
            var st = Motion.PlanMove(new Point(100, 300), new Point(460, 302), p, 24, false, Motion.Mulberry32(k), held: true);
            worst = Math.Max(worst, PathShape(new Point(100, 300), st).Dev);
            Assert.Equal((460.0, 302.0), (st[^1].X, st[^1].Y));
        }
        Assert.True(worst <= 8);
    }

    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_motion_vectors.json"))).RootElement.Clone();

    [Fact]
    public void Shared_vectors_shape_traits()
    {
        foreach (var t in Vec.GetProperty("shape_traits").EnumerateArray())
        {
            var st = Motion.ShapeTraitsOf(Motion.MakePersona(t.GetProperty("persona").GetString()));
            Assert.True(Math.Abs(st.OvershootRate - t.GetProperty("overshoot_rate").GetDouble()) < 1e-12);
            Assert.True(Math.Abs(st.Curve - t.GetProperty("curve").GetDouble()) < 1e-12);
            Assert.True(Math.Abs(st.DragTempo - t.GetProperty("drag_tempo").GetDouble()) < 1e-12);
        }
    }

    [Fact]
    public void Shared_vectors_paths()
    {
        foreach (var c in Vec.GetProperty("paths").EnumerateArray())
        {
            var f = c.GetProperty("from"); var to = c.GetProperty("to");
            string mode = c.GetProperty("mode").GetString()!;
            var steps = Motion.PlanMove(new Point(f[0].GetDouble(), f[1].GetDouble()), new Point(to[0].GetDouble(), to[1].GetDouble()),
                Motion.MakePersona(c.GetProperty("persona").GetString()), c.GetProperty("target_w").GetDouble(),
                mode == "settle", Motion.Mulberry32(c.GetProperty("rng").GetUInt32()), held: mode == "held");
            string label = $"{c.GetProperty("persona").GetString()} rng {c.GetProperty("rng").GetUInt32()} {mode}";
            Assert.True(steps.Count == c.GetProperty("n").GetInt32(), $"{label}: n {steps.Count}");
            Assert.True(Math.Abs(steps.Sum(s => s.SleepMs) - c.GetProperty("total_ms").GetDouble()) < 1e-6, label);
            foreach (var s in c.GetProperty("samples").EnumerateArray())
            {
                int i = s[0].GetInt32();
                Assert.True(Math.Abs(steps[i].X - s[1].GetDouble()) < 1e-6, $"{label} x[{i}]");
                Assert.True(Math.Abs(steps[i].Y - s[2].GetDouble()) < 1e-6, $"{label} y[{i}]");
                Assert.True(Math.Abs(steps[i].SleepMs - s[3].GetDouble()) < 1e-6, $"{label} sleep[{i}]");
            }
        }
    }
}
