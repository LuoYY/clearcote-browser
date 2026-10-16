using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Lane E (SC 232): the wheel anchor, drift, trackpad stream, drag-start hesitation, scroll distance and input
/// device pinned to the Python planners by sdk/testdata/humanize_scroll_vectors.json (Node reads the same file),
/// plus the trackpad stream's shape and the seed-fixes-traits-only rule. Before 0.52 .NET anchored at
/// 0.5/0.38 with sd 0.12/0.10 and a y clamp of 0.12-0.75, drifted N(0,2.2), and had no trackpad or hesitation.
/// </summary>
public class HumanizeDragScrollTests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_scroll_vectors.json"))).RootElement.Clone();

    private static void Close(double want, double got, double tol = 1e-6) => Assert.True(Math.Abs(want - got) < tol, $"{got} != {want}");

    [Fact]
    public void Anchor_and_drift_match_the_vectors()
    {
        foreach (var v in Vec.GetProperty("anchor").EnumerateArray())
        {
            var (x, y) = Scroll.AnchorPoint(v.GetProperty("vw").GetDouble(), v.GetProperty("vh").GetDouble(),
                Motion.Mulberry32((uint)v.GetProperty("rng").GetInt32()));
            Close(v.GetProperty("x").GetDouble(), x, 1e-9);
            Close(v.GetProperty("y").GetDouble(), y, 1e-9);
        }
        foreach (var v in Vec.GetProperty("drift").EnumerateArray())
        {
            var (dx, dy) = Scroll.WheelDrift(Motion.Mulberry32((uint)v.GetProperty("rng").GetInt32()));
            Close(v.GetProperty("dx").GetDouble(), dx, 1e-9);
            Close(v.GetProperty("dy").GetDouble(), dy, 1e-9);
        }
    }

    [Fact]
    public void Trackpad_stream_matches_the_vectors()
    {
        foreach (var v in Vec.GetProperty("trackpad").EnumerateArray())
        {
            var ev = Scroll.PlanTrackpad(v.GetProperty("dx").GetDouble(), v.GetProperty("dy").GetDouble(),
                Motion.Mulberry32((uint)v.GetProperty("rng").GetInt32()));
            Assert.Equal(v.GetProperty("n").GetInt32(), ev.Count);
            foreach (var s in v.GetProperty("samples").EnumerateArray())
            {
                int i = s[0].GetInt32();
                Close(s[1].GetDouble(), ev[i].Dx);
                Close(s[2].GetDouble(), ev[i].Dy);
                Close(s[3].GetDouble(), ev[i].SleepMs);
            }
        }
    }

    [Fact]
    public void Hesitation_and_held_drag_match_the_vectors()
    {
        foreach (var v in Vec.GetProperty("hesitation").EnumerateArray())
        {
            var f = v.GetProperty("from"); var t = v.GetProperty("to");
            double? dur = v.GetProperty("dur_ms").ValueKind == JsonValueKind.Null ? null : v.GetProperty("dur_ms").GetDouble();
            var steps = Motion.DragHesitation(new Point(f[0].GetDouble(), f[1].GetDouble()), new Point(t[0].GetDouble(), t[1].GetDouble()),
                Motion.MakePersona(v.GetProperty("persona").GetString()), Motion.Mulberry32((uint)v.GetProperty("rng").GetInt32()), dur);
            Assert.Equal(v.GetProperty("n").GetInt32(), steps.Count);
            foreach (var s in v.GetProperty("samples").EnumerateArray())
            {
                int i = s[0].GetInt32();
                Close(s[1].GetDouble(), steps[i].X); Close(s[2].GetDouble(), steps[i].Y); Close(s[3].GetDouble(), steps[i].SleepMs);
            }
        }
        foreach (var v in Vec.GetProperty("held").EnumerateArray())
        {
            var f = v.GetProperty("from"); var t = v.GetProperty("to");
            var (steps, creep) = Motion.HesitateThenDrag(new Point(f[0].GetDouble(), f[1].GetDouble()), new Point(t[0].GetDouble(), t[1].GetDouble()),
                Motion.MakePersona(v.GetProperty("persona").GetString()), v.GetProperty("window_ms").GetDouble(),
                v.GetProperty("settle").GetBoolean(), Motion.Mulberry32((uint)v.GetProperty("rng").GetInt32()));
            Assert.Equal(v.GetProperty("n").GetInt32(), steps.Count);
            Assert.Equal(v.GetProperty("creep").GetInt32(), creep);
            foreach (var s in v.GetProperty("samples").EnumerateArray())
            {
                int i = s[0].GetInt32();
                Close(s[1].GetDouble(), steps[i].X); Close(s[2].GetDouble(), steps[i].Y); Close(s[3].GetDouble(), steps[i].SleepMs);
            }
        }
    }

    [Fact]
    public void Scroll_need_and_device_match_the_vectors()
    {
        foreach (var v in Vec.GetProperty("need").EnumerateArray())
        {
            var b = v.GetProperty("box"); var w = v.GetProperty("view");
            var (dx, dy) = Scroll.ScrollNeed(new Box(b.GetProperty("x").GetDouble(), b.GetProperty("y").GetDouble(),
                    b.GetProperty("width").GetDouble(), b.GetProperty("height").GetDouble()),
                new Box(w[0].GetDouble(), w[1].GetDouble(), w[2].GetDouble(), w[3].GetDouble()), v.GetProperty("frac").GetDouble());
            Close(v.GetProperty("dx").GetDouble(), dx, 1e-9);
            Close(v.GetProperty("dy").GetDouble(), dy, 1e-9);
        }
        foreach (var v in Vec.GetProperty("device").EnumerateArray())
            Assert.Equal(v.GetProperty("device").GetString(),
                Scroll.InputDevice(v.GetProperty("platform").GetString(), v.GetProperty("option").GetString()));
    }

    [Fact]
    public void Trackpad_stream_shape()
    {
        for (int seed = 1; seed <= 200; seed++)
        {
            var sw = Scroll.PlanTrackpadStrokes(0, 120 + seed * 13, Motion.Mulberry32((uint)seed));
            foreach (var s in sw)
            {
                Assert.All(s.Finger, e => Assert.InRange(Math.Abs(e.Dy), 2 - 1e-9, 40));
                var tail = s.Tail.Select(e => e.Dy).ToList();
                for (int i = 1; i < tail.Count; i++) Assert.True(tail[i] < tail[i - 1]);
                Assert.True(s.Tail.Sum(e => e.SleepMs) >= 300);
                Assert.True(tail.Count == 0 || tail[^1] >= 1);
            }
            Close(120 + seed * 13, sw.Sum(s => s.Finger.Sum(e => e.Dy) + s.Tail.Sum(e => e.Dy)));
        }
    }

    [Fact]
    public void Seed_fixes_traits_but_every_action_draws_fresh()
    {
        var a = Motion.MakePersona("ident-1");
        var b = Motion.MakePersona("ident-1");
        var h1 = Motion.DragHesitation(new Point(100, 100), new Point(400, 120), a);
        var h2 = Motion.DragHesitation(new Point(100, 100), new Point(400, 120), b);
        Assert.NotEqual(h1.Select(s => s.X).ToArray(), h2.Select(s => s.X).ToArray());
        Assert.NotEqual(Scroll.PlanTrackpad(0, 500).Select(e => e.Dy).ToArray(), Scroll.PlanTrackpad(0, 500).Select(e => e.Dy).ToArray());
        Assert.NotEqual(Scroll.AnchorPoint(1280, 800), Scroll.AnchorPoint(1280, 800));
    }
}

/// <summary>
/// LIVE drag + scroll on sdk/testdata/humanize_dragscroll.html (skipped unless CLEARCOTE_LIVE_ENGINE names a
/// Clearcote build): wheel before every scroll, the target landing at 25-60 %, a nested container wheeled, a
/// non-scrollable panel reported, drag positions within 2 px with a 100-300 ms hesitation, a knob dragged by a
/// delta in at most 3 legs.
/// </summary>
public class HumanizeDragScrollLiveTests
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    private sealed record Ev(string t, double ts, double x, double y, double b, double dy, double mode, bool tr, string? id);

    private static async Task<List<Ev>> EventsAsync(IPage page, string kind)
    {
        var json = await page.EvaluateAsync<string>("JSON.stringify(window.__ev)");
        var all = JsonSerializer.Deserialize<List<JsonElement>>(json)!;
        double D(JsonElement e, string n) => e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;
        return all.Where(e => e.GetProperty("t").GetString() == kind).Select(e => new Ev(kind, D(e, "ts"), D(e, "x"), D(e, "y"), D(e, "b"),
            D(e, "dy"), D(e, "mode"), e.TryGetProperty("tr", out var tr) && tr.ValueKind == JsonValueKind.True,
            e.TryGetProperty("id", out var id) ? id.GetString() : null)).ToList();
    }

    [Fact]
    public async Task Wheel_scroll_drag_options_and_drag_by_on_a_real_page()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var html = File.ReadAllText(SharedTestData.PathOf("humanize_dragscroll.html"));
        var browser = await Clearcote.LaunchAsync(new LaunchOptions
        {
            ExecutablePath = LiveExe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true, Fingerprint = "dragscroll-live-net",
        });
        try
        {
            async Task<IPage> Fresh()
            {
                var pg = await browser.NewPageAsync(new BrowserNewPageOptions { ViewportSize = new ViewportSize { Width = 1280, Height = 800 } });
                Humanize.Attach(pg, "dragscroll-live-net");
                await pg.SetContentAsync(html);
                return pg;
            }

            var p = await Fresh();
            await p.HumanClickAsync("#far");
            var wheels = await EventsAsync(p, "wheel");
            var scrolls = await EventsAsync(p, "scroll");
            Assert.NotEmpty(wheels);
            Assert.True(scrolls.Count > 0 && scrolls[0].ts >= wheels[0].ts, "no scroll before the first wheel");
            Assert.Null(Humanize.LastFallback(p));
            var r = await p.EvaluateAsync<double[]>("(() => { const r = document.getElementById('far').getBoundingClientRect(); return [r.y, r.height]; })()");
            Assert.InRange((r[0] + r[1] / 2) / 800, 0.18, 0.67);
            Assert.Equal("far", (await EventsAsync(p, "click")).Last().id);
            await p.CloseAsync();

            p = await Fresh();
            await p.HumanClickAsync("#deep");
            Assert.True(await p.EvaluateAsync<double>("document.getElementById('box').scrollTop") > 0);
            Assert.Null(Humanize.LastFallback(p));
            await p.CloseAsync();

            p = await Fresh();
            await p.HumanClickAsync("#hid");
            Assert.Equal(new Humanize.FallbackEvent("scroll_into_view", "scroll into view: not_scrollable"), Humanize.LastFallback(p));
            await p.CloseAsync();

            p = await Fresh();
            await p.HumanDragAndDropAsync("#src", "#dst", new HumanDragOptions
            {
                SourcePosition = new Position { X = 10, Y = 12 }, TargetPosition = new Position { X = 180, Y = 20 },
            });
            var down = (await EventsAsync(p, "pointerdown")).Last();
            var up = (await EventsAsync(p, "pointerup")).Last();
            Assert.InRange(down.x, 108, 112); Assert.InRange(down.y, 210, 214);
            Assert.InRange(up.x, 878, 882); Assert.InRange(up.y, 438, 442);
            var held = (await EventsAsync(p, "move")).Where(m => m.b == 1 && m.ts >= down.ts && m.ts <= up.ts).ToList();
            Assert.True(held.Count >= 10);
            var leg = held.First(m => Math.Sqrt(Math.Pow(m.x - down.x, 2) + Math.Pow(m.y - down.y, 2)) >= 8);
            Assert.InRange(leg.ts - down.ts, 90, 380);
            await p.CloseAsync();

            p = await Fresh();
            var got = await p.HumanDragByAsync("#knob", 150, 0);
            var left = await p.EvaluateAsync<double>("document.getElementById('knob').offsetLeft");
            Assert.InRange(left - 2, 148, 152);
            Assert.InRange(got.X, 148, 152);
            Assert.InRange(Humanize.LastDragLegs(p), 1, 3);
            await p.CloseAsync();
        }
        finally
        {
            await browser.CloseAsync();
        }
    }
}
