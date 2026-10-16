using System.Reflection;
using System.Text.Json;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// SC 232 lane G (mirrors sdk/python/tests/test_motion_vectors_sc232.py): PlanDrag, the persona timings, PlanAmbient
/// and the wheel scroll-anchor model are pinned across the three SDKs by sdk/testdata/humanize_motion_vectors.json.
/// PlanAmbient is not ported to .NET on ceb7983 (lane C ports it) and the anchor has no pure function in any SDK
/// yet (lane E): those cases are checked through reflection once the method exists, and pass vacuously (logged
/// "API missing") until then.
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
        var m = typeof(Motion).GetMethod("PlanAmbient", BindingFlags.Public | BindingFlags.Static);
        if (m is null) { Console.WriteLine("SKIP (API missing): Motion.PlanAmbient (lane C)"); return; }
        foreach (var c in Vec.GetProperty("ambient").EnumerateArray())
        {
            var f = c.GetProperty("from"); var vp = c.GetProperty("viewport");
            var args = new List<object?> { new Point(f[0].GetDouble(), f[1].GetDouble()) };
            // PlanAmbient(Point from, (W,H) viewport | Box | ..., Persona p, double ms, Rng rng): resolve the viewport
            // parameter by its type so the port's exact viewport type does not matter.
            var ps = m.GetParameters();
            object? view = ps[1].ParameterType == typeof(Box) ? new Box(0, 0, vp.GetProperty("width").GetDouble(), vp.GetProperty("height").GetDouble())
                : Activator.CreateInstance(ps[1].ParameterType, vp.GetProperty("width").GetDouble(), vp.GetProperty("height").GetDouble());
            args.Add(view);
            args.Add(Motion.MakePersona(c.GetProperty("persona").GetString()));
            args.Add(c.GetProperty("ms").GetDouble());
            args.Add(Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32()));
            var steps = (IReadOnlyList<Step>)m.Invoke(null, args.ToArray())!;
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
        var m = typeof(Motion).GetMethod("WheelAnchorPoint", BindingFlags.Public | BindingFlags.Static);
        if (m is null) { Console.WriteLine("SKIP (API missing): Motion.WheelAnchorPoint (lane E)"); return; }
        foreach (var c in Vec.GetProperty("wheel_anchor").GetProperty("cases").EnumerateArray())
        {
            var r = Motion.Mulberry32((uint)c.GetProperty("rng").GetInt32());
            var vp = c.GetProperty("viewport");
            foreach (var pt in c.GetProperty("points").EnumerateArray())
            {
                var got = (Point)m.Invoke(null, new object?[] { vp[0].GetDouble(), vp[1].GetDouble(), r })!;
                Assert.Equal(pt[0].GetDouble(), got.X, 6);
                Assert.Equal(pt[1].GetDouble(), got.Y, 6);
            }
        }
    }
}
