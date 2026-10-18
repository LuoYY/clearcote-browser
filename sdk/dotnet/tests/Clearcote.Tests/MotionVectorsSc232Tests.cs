using System.Reflection;
using System.Text.Json;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// SC 232 lane G (mirrors sdk/python/tests/test_motion_vectors_sc232.py): PlanDrag, the persona timings, PlanAmbient
/// and the wheel scroll-anchor model are pinned across the three SDKs by sdk/testdata/humanize_motion_vectors.json.
/// PlanAmbient (ported by lane C) and the pure anchor Motion.WheelAnchorPoint (lane E) are called directly since the
/// SC 232 merge; before it those cases passed vacuously (logged "API missing").
/// </summary>
public class MotionVectorsSc232Tests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_motion_vectors.json"))).RootElement.Clone();

    private static void Check(IReadOnlyList<Step> steps, JsonElement c, string what)
    {
        Assert.Equal(c.GetProperty("n").GetInt32(), steps.Count);
        Assert.True(Math.Abs(steps.Sum(s => s.SleepMs) - c.GetProperty("total_ms").GetDouble()) < 1e-6, what + " total");
        foreach (var smp in c.GetProperty("samples").EnumerateArray())
        {
            int i = smp[0].GetInt32();
            Assert.True(Math.Abs(steps[i].X - smp[1].GetDouble()) < 1e-6, $"{what} x[{i}]");
            Assert.True(Math.Abs(steps[i].Y - smp[2].GetDouble()) < 1e-6, $"{what} y[{i}]");
            Assert.True(Math.Abs(steps[i].SleepMs - smp[3].GetDouble()) < 1e-6, $"{what} sleep[{i}]");
        }
    }

    [Fact]
    public void PlanDrag_matches_vectors()
    {
        foreach (var c in Vec.GetProperty("drags").EnumerateArray())
        {
            var f = c.GetProperty("from"); var t = c.GetProperty("to");
            var steps = Motion.PlanDrag(new Point(f[0].GetDouble(), f[1].GetDouble()), new Point(t[0].GetDouble(), t[1].GetDouble()),
                Motion.MakePersona(c.GetProperty("persona").GetString()), c.GetProperty("settle").GetBoolean(), Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32()));
            Check(steps, c, "drag " + c.GetProperty("persona").GetString());
        }
    }

    [Fact]
    public void Timings_match_vectors()
    {
        foreach (var c in Vec.GetProperty("timings").EnumerateArray())
        {
            var p = Motion.MakePersona(c.GetProperty("persona").GetString());
            var r = Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32());
            var (g, rel) = Motion.DragDwell(p, r);
            Assert.Equal(c.GetProperty("grab_ms").GetDouble(), g, 9);
            Assert.Equal(c.GetProperty("release_ms").GetDouble(), rel, 9);
            Assert.Equal(c.GetProperty("click_hold").GetDouble(), Motion.ClickHold(p, r), 9);
            Assert.Equal(c.GetProperty("key_dwell").GetDouble(), Motion.KeyDwell(p, r), 9);
            Assert.Equal(c.GetProperty("rollover_rate").GetDouble(), Motion.RolloverRate(p), 12);
        }
    }

    [Fact]
    public void PlanAmbient_matches_vectors_when_ported()
    {
        // Lane C ported PlanAmbient (Point from, double vw, double vh, Persona, double ms, Rng): called directly now.
        foreach (var c in Vec.GetProperty("ambient").EnumerateArray())
        {
            var f = c.GetProperty("from"); var vp = c.GetProperty("viewport");
            var steps = Motion.PlanAmbient(new Point(f[0].GetDouble(), f[1].GetDouble()),
                vp.GetProperty("width").GetDouble(), vp.GetProperty("height").GetDouble(),
                Motion.MakePersona(c.GetProperty("persona").GetString()), c.GetProperty("ms").GetDouble(),
                Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32()));
            Check(steps, c, "ambient " + c.GetProperty("persona").GetString());
        }
    }

    [Fact]
    public void Wheel_anchor_model_and_points()
    {
        var model = Vec.GetProperty("wheel_anchor").GetProperty("model");
        Assert.Equal(0.5, model.GetProperty("x").GetProperty("mean").GetDouble());
        Assert.Equal(0.34, model.GetProperty("y").GetProperty("mean").GetDouble());
        Assert.Equal(140, model.GetProperty("target_w").GetInt32());
        foreach (var c in Vec.GetProperty("wheel_anchor").GetProperty("cases").EnumerateArray())
        {
            var r = Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32());
            var vp = c.GetProperty("viewport");
            foreach (var pt in c.GetProperty("points").EnumerateArray())
            {
                var got = Motion.WheelAnchorPoint(vp[0].GetDouble(), vp[1].GetDouble(), r);   // lane E: called directly
                Assert.Equal(pt[0].GetDouble(), got.X, 6);
                Assert.Equal(pt[1].GetDouble(), got.Y, 6);
            }
        }
    }

    /// <summary>SC 239: a fresh page's first glide — EntryPoint → PlanMove → EnterFromTop with ONE rng in that order —
    /// pinned across the SDKs (the "entry" section).</summary>
    [Fact]
    public void Entry_vectors_match_the_pipeline()
    {
        foreach (var c in Vec.GetProperty("entry").EnumerateArray())
        {
            var r = Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32());
            var tg = c.GetProperty("target");
            var target = new Point(tg[0].GetDouble(), tg[1].GetDouble());
            var from = Motion.EntryPoint(target, c.GetProperty("view_w").GetDouble(), r);
            var f = c.GetProperty("from");
            Assert.Equal(f[0].GetDouble(), from.X, 6);
            Assert.Equal(f[1].GetDouble(), from.Y, 6);
            var planned = Motion.PlanMove(from, target, Motion.MakePersona(c.GetProperty("persona").GetString()),
                targetW: c.GetProperty("target_w").GetDouble(), rng: r);
            Assert.Equal(c.GetProperty("planned_n").GetInt32(), planned.Count);
            var steps = Motion.EnterFromTop(planned, from, r);
            Assert.Equal(c.GetProperty("n").GetInt32(), steps.Count);
            foreach (var smp in c.GetProperty("samples").EnumerateArray())
            {
                int i = smp[0].GetInt32();
                Assert.Equal(smp[1].GetDouble(), steps[i].X, 6);
                Assert.Equal(smp[2].GetDouble(), steps[i].Y, 6);
                Assert.Equal(smp[3].GetDouble(), steps[i].SleepMs, 6);
            }
        }
    }

    /// <summary>SC 239: an ambient drift on a fresh page — EntryPoint → PlanAmbient(ms + EntryLeadMs) → EnterFromTop with
    /// ONE rng in that order (Humanize.AmbientPlan) — pinned across the SDKs (the "entry_ambient" section).</summary>
    [Fact]
    public void Entry_ambient_vectors_match_the_pipeline()
    {
        foreach (var c in Vec.GetProperty("entry_ambient").EnumerateArray())
        {
            var r = Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32());
            var pos = c.GetProperty("pos");
            var view = c.GetProperty("view");
            double vw = view[0].GetDouble(), vh = view[1].GetDouble();
            var start = Motion.EntryPoint(new Point(pos[0].GetDouble(), pos[1].GetDouble()), vw, r);
            var s0 = c.GetProperty("start");
            Assert.Equal(s0[0].GetDouble(), start.X, 6);
            Assert.Equal(s0[1].GetDouble(), start.Y, 6);
            var planned = Motion.PlanAmbient(start, vw, vh, Motion.MakePersona(c.GetProperty("persona").GetString()),
                c.GetProperty("ms").GetDouble() + Motion.EntryLeadMs, r);
            Assert.Equal(c.GetProperty("planned_n").GetInt32(), planned.Count);
            var steps = Motion.EnterFromTop(planned, start, r);
            Assert.Equal(c.GetProperty("n").GetInt32(), steps.Count);
            Assert.InRange(steps[0].Y, 0, Motion.EntryCrossPx);
            Assert.Equal(c.GetProperty("total_ms").GetDouble(), steps.Sum(s => s.SleepMs), 3);
            foreach (var smp in c.GetProperty("samples").EnumerateArray())
            {
                int i = smp[0].GetInt32();
                Assert.Equal(smp[1].GetDouble(), steps[i].X, 6);
                Assert.Equal(smp[2].GetDouble(), steps[i].Y, 6);
                Assert.Equal(smp[3].GetDouble(), steps[i].SleepMs, 6);
            }
        }
    }
}
