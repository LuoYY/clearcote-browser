using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Lane D (SC 232): the keyboard planner (Keys.cs) against the SHARED vectors in
/// sdk/testdata/humanize_typing_vectors.json -- the file the Python (test_keys.py) and Node (keys.test.ts) suites
/// read -- and the humanized keyboard wired through it: the cadence of HumanTypeAsync against the shared KS
/// reference, same-finger vs alternating bigrams, chords, OS auto-repeat, the submit pause, the caller's delay as a
/// floor, held modifiers and the persona's typo rate. Every sleep of the keyboard path goes through
/// Humanize.KeySleep, which these tests replace with a fake clock. On 0.51.0 there was no Keys class, and the type
/// loop drew max(25, N(85, 45)) for every persona.
public class KeysTests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_typing_vectors.json"))).RootElement.Clone();

    private static string Pascal(string snake) => string.Concat(snake.Split('_').Select(w => char.ToUpperInvariant(w[0]) + w[1..]));
    private static object SeedOf(JsonElement e) => e.ValueKind == JsonValueKind.Number ? e.GetDouble() : e.GetString()!;
    private static TypingTraits TraitsOf(string p) => Keys.TypingTraitsOf(Motion.MakePersona(p).Seed);
    private static double[] Nums(JsonElement a) => a.EnumerateArray().Select(x => x.GetDouble()).ToArray();
    private static void Close(IReadOnlyList<double> got, IReadOnlyList<double> want, double tol = 1e-9)
    {
        Assert.Equal(want.Count, got.Count);
        for (int i = 0; i < got.Count; i++) Assert.True(Math.Abs(got[i] - want[i]) <= tol, $"#{i}: {got[i]} vs {want[i]}");
    }

    [Fact]
    public void Make_persona_fields_are_unchanged()
    {
        foreach (var c in Vec.GetProperty("persona_pin").EnumerateArray())
        {
            var p = Motion.MakePersona(SeedOf(c.GetProperty("input")));
            foreach (var f in c.GetProperty("persona").EnumerateObject())
            {
                var prop = typeof(Persona).GetProperty(Pascal(f.Name));
                if (prop is null) continue;
                Assert.Equal(f.Value.GetDouble(), Convert.ToDouble(prop.GetValue(p)), 12);
            }
        }
    }

    [Fact]
    public void Typing_traits_vectors()
    {
        foreach (var c in Vec.GetProperty("typing_traits").EnumerateArray())
        {
            var t = TraitsOf(c.GetProperty("persona").GetString()!);
            foreach (var f in c.EnumerateObject().Where(f => f.Name != "persona"))
            {
                var v = typeof(TypingTraits).GetProperty(Pascal(f.Name))!.GetValue(t);
                if (f.Value.ValueKind == JsonValueKind.Number) Assert.Equal(f.Value.GetDouble(), (double)v!, 12);
                else Assert.Equal(f.Value.GetString(), (string)v!);
            }
        }
    }

    [Fact]
    public void Finger_map_and_bigram_classes()
    {
        foreach (var c in Vec.GetProperty("finger_of").EnumerateArray())
        {
            var got = Keys.FingerOf(c.GetProperty("ch").GetString());
            var want = c.GetProperty("want");
            if (want.ValueKind == JsonValueKind.Null) Assert.Null(got);
            else Assert.Equal((want[0].GetInt32(), want[1].GetInt32()), got!.Value);
        }
        foreach (var c in Vec.GetProperty("bigram_class").EnumerateArray())
            Assert.Equal((c.GetProperty("cls").GetString()!, c.GetProperty("jump").GetBoolean()),
                Keys.BigramClass(c.GetProperty("prev").GetString(), c.GetProperty("ch").GetString()));
    }

    [Fact]
    public void Planned_flights_and_gaps()
    {
        foreach (var c in Vec.GetProperty("gaps").EnumerateArray())
        {
            var t = TraitsOf(c.GetProperty("persona").GetString()!);
            var s = Forms.Chars(c.GetProperty("text").GetString()!);
            uint rs = c.GetProperty("rng").GetUInt32();
            var r = Motion.Mulberry32(rs);
            Close(Enumerable.Range(0, s.Length - 1).Select(i => Keys.FlightMs(t, s[i], s[i + 1], r)).ToList(), Nums(c.GetProperty("flight_ms")));
            r = Motion.Mulberry32(rs);
            Close(Enumerable.Range(0, s.Length - 1).Select(i => Keys.PlanGap(t, s[i], s[i + 1], r)).ToList(), Nums(c.GetProperty("gap_ms")));
        }
    }

    [Fact]
    public void Chords_repeat_and_pauses()
    {
        foreach (var c in Vec.GetProperty("split_chord").EnumerateArray())
        {
            var (mods, key) = Keys.SplitChord(c.GetProperty("key").GetString()!);
            Assert.Equal(c.GetProperty("mods").EnumerateArray().Select(x => x.GetString()!).ToArray(), mods);
            Assert.Equal(c.GetProperty("key_out").GetString(), key);
        }
        foreach (var c in Vec.GetProperty("timing").EnumerateArray())
        {
            var t = TraitsOf(c.GetProperty("persona").GetString()!);
            var r = Motion.Mulberry32(c.GetProperty("rng").GetUInt32());
            var rp = Keys.PlanRepeat(r);
            Close(new[] { rp.FirstMs, rp.GapMs, rp.ReleaseMs }, Nums(c.GetProperty("repeat")));
            Close(new[] { Keys.ModifierLeadMs(r), Keys.ModifierLagMs(r) }, new[] { c.GetProperty("lead").GetDouble(), c.GetProperty("lag").GetDouble() });
            Close(Enumerable.Range(0, 4).Select(_ => Keys.SubmitPauseMs(t, r)).ToList(), Nums(c.GetProperty("submit")));
            Close(Enumerable.Range(0, 4).Select(_ => Keys.TabPauseMs(t, r)).ToList(), Nums(c.GetProperty("tab")));
        }
    }

    [Fact]
    public void The_ks_reference_is_this_planner()
    {
        var t = TraitsOf(Vec.GetProperty("ks_persona").GetString()!);
        var r = Motion.Mulberry32(Vec.GetProperty("ks_rng").GetUInt32());
        var s = Forms.Chars(Vec.GetProperty("ks_text").GetString()!);
        Close(Enumerable.Range(0, s.Length - 1).Select(i => Keys.PlanGap(t, s[i], s[i + 1], r)).ToList(), Nums(Vec.GetProperty("ks_reference")));
    }

    // ------------------------------------------------------------------ the wrapper on a fake clock
    public class Kb : HumanizeShiftTests.Recorder
    {
        public double Clock;
        public readonly List<(string Op, string Key, double T, double Delay, bool Rep, string[] Held)> Ev = new();
        private readonly HashSet<string> _held = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.DeclaringType == typeof(IKeyboard))
            {
                var k = args is { Length: > 0 } ? args[0] as string ?? "" : "";
                switch (method.Name)
                {
                    case "PressAsync":
                        var d = (args!.Length > 1 ? (args[1] as KeyboardPressOptions)?.Delay : null) ?? 0;
                        Ev.Add(("press", k, Clock, d, false, _held.ToArray()));
                        return Task.CompletedTask;
                    case "DownAsync":
                        Ev.Add(("down", k, Clock, 0, _held.Contains(k), _held.ToArray()));
                        _held.Add(k);
                        return Task.CompletedTask;
                    case "UpAsync":
                        Ev.Add(("up", k, Clock, 0, false, _held.ToArray()));
                        _held.Remove(k);
                        return Task.CompletedTask;
                }
            }
            return base.Invoke(method, args);
        }
    }

    private static (IPage Page, Kb Kb) FakePage(string seed, double typoRate = 0, bool roll = false)
    {
        var keyboard = DispatchProxy.Create<IKeyboard, Kb>();
        var mouse = DispatchProxy.Create<IMouse, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Keyboard = keyboard;
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        Humanize.Attach(page, seed);
        if (!roll) Humanize.RolloverOverride.AddOrUpdate(page, new StrongBox<double>(0));
        if (typoRate >= 0) Humanize.TypoRateOverride.AddOrUpdate(page, new StrongBox<double>(typoRate));
        var kb = (Kb)(object)keyboard;
        Humanize.KeySleep = ms => { kb.Clock += ms; return Task.CompletedTask; };
        return (page, kb);
    }

    private static async Task<T> WithFakeClock<T>(Func<Task<T>> f)
    {
        var saved = Humanize.KeySleep;
        try { return await f(); }
        finally { Humanize.KeySleep = saved; }
    }

    private static double Ks(IReadOnlyList<double> a, IReadOnlyList<double> b)
    {
        var x = a.OrderBy(v => v).ToArray();
        var y = b.OrderBy(v => v).ToArray();
        int i = 0, j = 0;
        double d = 0;
        while (i < x.Length && j < y.Length)
        {
            double v = Math.Min(x[i], y[j]);
            while (i < x.Length && x[i] == v) i++;
            while (j < y.Length && y[j] == v) j++;
            d = Math.Max(d, Math.Abs((double)i / x.Length - (double)j / y.Length));
        }
        return d;
    }

    private static double Median(IEnumerable<double> xs)
    {
        var s = xs.OrderBy(v => v).ToArray();
        return s.Length % 2 == 1 ? s[s.Length / 2] : (s[s.Length / 2 - 1] + s[s.Length / 2]) / 2;
    }

    private static List<(string Op, string Key, double T, double Delay, bool Rep, string[] Held)> CharPresses(Kb kb)
        => kb.Ev.Where(e => e.Op == "press" && e.Key.Length == 1).ToList();

    [Fact]
    public async Task Key_gaps_match_the_shared_reference_distribution()
    {
        var gaps = await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage(Vec.GetProperty("ks_persona").GetString()!);
            await page.HumanTypeAsync(Vec.GetProperty("ks_text").GetString()!);
            var p = CharPresses(kb);
            return p.Skip(1).Select((e, i) => e.T - p[i].T).ToList();   // a fake press takes no time
        });
        Assert.Equal(500, gaps.Count);
        // Task.Delay sleeps whole ms; the fake clock keeps the planned value
        double d = Ks(gaps, Nums(Vec.GetProperty("ks_reference")));
        Assert.True(d < 1.95 * Math.Sqrt(2.0 / 500), $"KS D={d:F3}");
    }

    private static readonly string[] SameFinger = { "ed", "de", "ju", "ki", "lo", "fr", "sw", "ol" };
    private static readonly string[] Alternate = { "en", "th", "ha", "so", "al", "ka", "fj", "dk" };

    [Fact]
    public async Task Same_finger_bigrams_are_slower_than_alternating()
    {
        var ratios = new List<double>();
        foreach (var seed in new[] { "ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6" })
        {
            var rnd = new Random(seed.GetHashCode());
            var words = Enumerable.Repeat(SameFinger.Concat(Alternate), 25).SelectMany(w => w).OrderBy(_ => rnd.Next()).ToArray();
            var r = await WithFakeClock(async () =>
            {
                var (page, kb) = FakePage(seed);
                await page.HumanTypeAsync(string.Join(" ", words));
                var p = CharPresses(kb);
                var sf = new List<double>();
                var alt = new List<double>();
                for (int i = 1; i < p.Count; i++)
                {
                    var pair = p[i - 1].Key + p[i].Key;
                    double iv = p[i].T - p[i - 1].T + p[i - 1].Delay;   // keydown to keydown
                    if (SameFinger.Contains(pair)) sf.Add(iv); else if (Alternate.Contains(pair)) alt.Add(iv);
                }
                return Median(sf) / Median(alt);
            });
            ratios.Add(r);
        }
        double m = Median(ratios);
        Assert.True(m is >= 1.2 and <= 1.8, string.Join(",", ratios));
        Assert.All(ratios, x => Assert.True(x >= 1.15, string.Join(",", ratios)));
    }

    [Fact]
    public async Task A_chord_presses_the_modifier_first_and_lifts_it_last()
    {
        var ev = await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage("ha-1");
            for (int j = 0; j < 10; j++) await page.HumanPressAsync("Control+a");
            await page.HumanPressAsync("ControlOrMeta+Shift+ArrowLeft");
            return kb.Ev.ToList();
        });
        for (int j = 0; j < 10; j++)
        {
            var (d, p, u) = (ev[3 * j], ev[3 * j + 1], ev[3 * j + 2]);
            Assert.Equal(("down", "Control", "press", "a", "up", "Control"), (d.Op, d.Key, p.Op, p.Key, u.Op, u.Key));
            Assert.InRange(p.T - d.T, 40, 110);
            Assert.InRange(u.T - p.T, 20, 80);
            Assert.True(p.Delay >= 45);
        }
        Assert.Equal(new[] { "down:ControlOrMeta", "down:Shift", "press:ArrowLeft", "up:Shift", "up:ControlOrMeta" },
            ev.Skip(30).Select(e => $"{e.Op}:{e.Key}").ToArray());
    }

    [Fact]
    public async Task A_modifier_the_caller_holds_is_honoured()
    {
        var ev = await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage("ha-1");
            await page.HumanKeyDownAsync("Shift");
            await page.HumanPressAsync("Shift+a");
            await page.HumanTypeAsync("bc");
            await page.HumanKeyUpAsync("Shift");
            return kb.Ev.ToList();
        });
        Assert.Single(ev, e => e.Op == "down" && e.Key == "Shift");
        Assert.Equal(new[] { "a", "b", "c" }, ev.Where(e => e.Op == "press").Select(e => e.Key).ToArray());
    }

    [Fact]
    public async Task Repeat_is_an_os_auto_repeat()
    {
        var ev = await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage("ha-2");
            await page.HumanPressAsync("ArrowRight", repeat: 12);
            return kb.Ev.ToList();
        });
        var downs = ev.Where(e => e.Op == "down").ToList();
        Assert.Equal(12, downs.Count);
        Assert.Equal(new[] { false }.Concat(Enumerable.Repeat(true, 11)), downs.Select(e => e.Rep));
        Assert.Single(ev, e => e.Op == "up");
        var gaps = downs.Skip(1).Select((e, i) => e.T - downs[i].T).ToList();
        Assert.InRange(gaps[0], 250, 500);
        Assert.All(gaps.Skip(1), g => Assert.InRange(g, 25, 40));
    }

    [Fact]
    public async Task Enter_after_typing_waits_the_submit_pause()
    {
        foreach (var seed in new[] { "ha-1", "ha-4" })
        {
            var ev = await WithFakeClock(async () =>
            {
                var (page, kb) = FakePage(seed);
                await page.HumanTypeAsync("hello");
                await page.HumanPressAsync("Enter");
                return kb.Ev.Where(e => e.Op == "press").ToList();
            });
            Assert.InRange(ev[^1].T - ev[^2].T, 230, 900);   // less the real ms spent between the calls
        }
    }

    [Fact]
    public async Task The_callers_delay_is_a_floor()
    {
        var p = await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage("ha-5", roll: true);
            await page.HumanTypeAsync("abcdefgh ijkl", delay: 300);
            return CharPresses(kb);
        });
        Assert.Equal(13, p.Count);
        // 1e-6: the fake clock sums float sleeps (299.9999999999999 seen after the merge)
        for (int i = 1; i < p.Count; i++) Assert.True(p[i].T - p[i - 1].T >= 300 - 1e-6, $"{p[i].T - p[i - 1].T}");
    }

    [Fact]
    public async Task The_typo_rate_is_the_personas()
    {
        using var sb = new Sandbox();
        sb.Env(Humanize.TypoRateEnv, null);
        const string text = "we keep a quiet mind and type plain words into the form until the day ends then rest slowly between lines ";
        var full = string.Concat(Enumerable.Repeat(text, 12));
        int eligible = full.Count(char.IsLetterOrDigit);
        var got = new Dictionary<string, (double Got, double Want)>();
        foreach (var seed in new[] { "ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6" })
        {
            int slips = 0;
            for (int run = 0; run < 3; run++)
            {
                slips += await WithFakeClock(async () =>
                {
                    var (page, kb) = FakePage(seed, typoRate: -1);   // no override: the persona's rate
                    await page.HumanTypeAsync(full);
                    int n = 0;
                    bool prev = false;
                    foreach (var e in kb.Ev.Where(e => e.Op == "press"))
                    {
                        bool bs = e.Key == "Backspace";
                        if (bs && !prev) n++;
                        prev = bs;
                    }
                    return n;
                });
            }
            double r = slips / (3.0 * eligible), want = Motion.TimingTraitsOf(Motion.MakePersona(seed)).TypoRate;   // lane B: the single source
            got[seed] = (r, want);
            Assert.True(r >= Math.Max(0.003, 0.4 * want) && r <= Math.Min(0.05, 1.9 * want + 0.002),
                string.Join(" ", got.Select(kv => $"{kv.Key}={kv.Value.Got:F4}/{kv.Value.Want:F4}")));
        }
    }

    [Fact]
    public async Task Same_seed_same_traits_but_fresh_per_key_draws()
    {
        async Task<double[]> Run() => await WithFakeClock(async () =>
        {
            var (page, kb) = FakePage("ha-1");
            await page.HumanTypeAsync("the same words typed twice");
            return CharPresses(kb).Select(e => e.T).ToArray();
        });
        Assert.NotEqual(await Run(), await Run());
        Assert.Equal(TraitsOf("ha-1"), TraitsOf("ha-1"));
    }

    [Fact]
    public async Task Lane_a_tempo_scales_the_gaps()
    {
        // lane A's presets will put Tempo on the persona; until then the planner's tempo parameter is what scales
        var t = TraitsOf("ha-2");
        var s = Forms.Chars(Vec.GetProperty("ks_text").GetString()!);
        var r1 = Motion.Mulberry32(3);
        var r2 = Motion.Mulberry32(3);
        var a = Enumerable.Range(0, 200).Select(i => Keys.PlanGap(t, s[i], s[i + 1], r1)).ToArray();
        var b = Enumerable.Range(0, 200).Select(i => Keys.PlanGap(t, s[i], s[i + 1], r2, tempo: 2)).ToArray();
        for (int i = 0; i < a.Length; i++) Assert.Equal(2 * a[i], b[i], 9);
        await Task.CompletedTask;
    }
}
