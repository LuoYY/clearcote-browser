using System.Globalization;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

/// Files in sdk/testdata, shared by the Python, Node and .NET suites, found by walking up from the test
/// binary (the same way FontsTests finds assets/fonts).
internal static class SharedTestData
{
    public static string PathOf(string name)
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            foreach (var cand in new[] { Path.Combine(dir.FullName, "testdata", name), Path.Combine(dir.FullName, "sdk", "testdata", name) })
                if (File.Exists(cand)) return cand;
        }
        throw new FileNotFoundException($"sdk/testdata/{name} not found above the test binary");
    }
}

/// Humanized form input planning (Forms.cs) against the SHARED vectors in
/// sdk/testdata/humanize_forms_vectors.json -- the same file the Python and Node suites read, so the three
/// SDKs cannot drift apart -- plus the property checks the vectors cannot express (a port of
/// sdk/python/tests/test_forms.py).
public class FormsTests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_forms_vectors.json"))).RootElement.Clone();

    public static IEnumerable<object[]> Cases(string key)
        => Enumerable.Range(0, Vec.GetProperty(key).GetArrayLength()).Select(i => new object[] { i });

    private static JsonElement Case(string key, int i) => Vec.GetProperty(key)[i];

    private static string? Str(JsonElement c, string name)
        => c.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool Has(JsonElement c, string name) => c.TryGetProperty(name, out _);

    private static List<string?> Labels(JsonElement c)
        => c.GetProperty("labels").EnumerateArray().Select(l => l.ValueKind == JsonValueKind.Null ? null : l.GetString()).ToList();

    // ---------------------------------------------------------------------------------- vectors
    [Theory, MemberData(nameof(Cases), "classify_field")]
    public void Classify_field(int i)
    {
        var c = Case("classify_field", i);
        Assert.Equal(Str(c, "want"), Forms.ClassifyField(Str(c, "tag"), Str(c, "type"), c.GetProperty("ce").GetBoolean()));
    }

    [Theory, MemberData(nameof(Cases), "typos_allowed")]
    public void Typos_allowed(int i)
    {
        var c = Case("typos_allowed", i);
        var f = c.GetProperty("field");
        var field = f.ValueKind == JsonValueKind.Null ? null : new FieldTraits(Str(f, "type"), Str(f, "inputmode"), Str(f, "autocomplete"),
            f.TryGetProperty("frame", out var fr) && fr.ValueKind == JsonValueKind.True);
        Assert.Equal(c.GetProperty("want").GetBoolean(), Forms.TyposAllowed(field));
    }

    [Theory, MemberData(nameof(Cases), "formatted_equal")]
    public void Formatted_equal(int i)
    {
        var c = Case("formatted_equal", i);
        Assert.Equal(c.GetProperty("result").GetBoolean(), Forms.FormattedEqual(Str(c, "got"), Str(c, "want")));
    }

    [Theory, MemberData(nameof(Cases), "range_value_ok")]
    public void Range_value_ok(int i)
    {
        var c = Case("range_value_ok", i);
        Assert.Equal(c.GetProperty("want").GetBoolean(), Forms.RangeValueOk(Str(c, "value"), Str(c, "min"), Str(c, "max"), Str(c, "step")));
    }

    // Every group in the shared vectors file has a test here: a group added for another SDK cannot be
    // silently skipped by this one.
    private static readonly string[] CoveredGroups =
    {
        "classify_field", "typos_allowed", "typo_eligible", "formatted_equal", "nearby_digits", "typeahead_simulate",
        "plan_select", "match_option", "range_sanitize", "range_value_ok", "range_point", "range_nudge", "plan_datetime",
        "datetime_equal", "normalize_click_options", "engine_path_ok",
    };

    [Fact]
    public void Every_vector_group_is_tested()
    {
        var groups = Vec.EnumerateObject().Select(p => p.Name).Where(n => !n.StartsWith('_')).ToList();
        Assert.Empty(groups.Except(CoveredGroups));
    }

    [Fact]
    public void Range_sanitize_always_returns_quickly_on_hostile_attributes()
    {
        // A page chooses min/max/step: no combination may make the planner spin (it used to walk down in a
        // loop that never ended once a step was smaller than the double spacing at the value).
        var rng = new Random(11);
        double[] his = { 1e15, 123456789, 1e300, 5, 1e-300 }, sts = { 1e-9, 1e-30, 1e-300, 0.5, 7 };
        var sw = System.Diagnostics.Stopwatch.StartNew();
        for (int n = 0; n < 5000; n++)
        {
            double hi = his[rng.Next(his.Length)], st = sts[rng.Next(sts.Length)];
            double[] vs = { hi, hi * 10, -hi, 0.0, 1e308 };
            double v = Forms.RangeSanitize(vs[rng.Next(vs.Length)], "0", R(hi), R(st));
            Assert.False(double.IsNaN(v));
        }
        Assert.True(sw.Elapsed.TotalSeconds < 5);
    }

    [Theory, MemberData(nameof(Cases), "typo_eligible")]
    public void Typo_eligible(int i)
    {
        var c = Case("typo_eligible", i);
        Assert.Equal(c.GetProperty("want").GetBoolean(), Forms.TypoEligible(Str(c, "ch")));
    }

    [Fact]
    public void Digit_neighbours_are_the_shared_map()
    {
        var want = Vec.GetProperty("nearby_digits").EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetString());
        Assert.Equal(want.OrderBy(k => k.Key), Forms.NearbyDigits.OrderBy(k => k.Key).Select(k => KeyValuePair.Create(k.Key, (string?)k.Value)));
    }

    [Theory, MemberData(nameof(Cases), "typeahead_simulate")]
    public void Typeahead_simulate(int i)
    {
        var c = Case("typeahead_simulate", i);
        Assert.Equal(c.GetProperty("want").GetInt32(),
            Forms.TypeaheadSimulate(Labels(c), c.GetProperty("start").GetInt32(), Str(c, "keys")!));
    }

    [Theory, MemberData(nameof(Cases), "plan_select")]
    public void Plan_select(int i)
    {
        var c = Case("plan_select", i);
        var got = Forms.PlanSelect(Labels(c), c.GetProperty("current").GetInt32(), c.GetProperty("target").GetInt32());
        var want = c.GetProperty("want");
        if (want.ValueKind == JsonValueKind.Null) Assert.Null(got);
        else Assert.Equal(new SelectPlan(want.GetProperty("keys").GetString()!, want.GetProperty("arrows").GetInt32()), got);
    }

    [Theory, MemberData(nameof(Cases), "match_option")]
    public void Match_option(int i)
    {
        var c = Case("match_option", i);
        var opts = c.GetProperty("options").EnumerateArray().Select(o => new OptionInfo(Str(o, "value"), Str(o, "label"))).ToList();
        int? index = Has(c, "index") ? c.GetProperty("index").GetInt32() : null;
        Assert.Equal(c.GetProperty("want").GetInt32(), Forms.MatchOption(opts, Str(c, "value"), index, Str(c, "label")));
    }

    [Theory, MemberData(nameof(Cases), "range_sanitize")]
    public void Range_sanitize(int i)
    {
        var c = Case("range_sanitize", i);
        double got = Forms.RangeSanitize(Str(c, "value"), Str(c, "min"), Str(c, "max"), Str(c, "step"));
        double want = c.GetProperty("want").GetDouble(), tol = Math.Max(1e-12 * Math.Abs(want), 1e-9);   // pytest.approx(rel=1e-12, abs=1e-9)
        Assert.InRange(got, want - tol, want + tol);
    }

    [Theory, MemberData(nameof(Cases), "range_point")]
    public void Range_point(int i)
    {
        var c = Case("range_point", i);
        var b = c.GetProperty("box");
        var box = new Box(b.GetProperty("x").GetDouble(), b.GetProperty("y").GetDouble(), b.GetProperty("width").GetDouble(), b.GetProperty("height").GetDouble());
        var (x, y) = Forms.RangePoint(box, c.GetProperty("fraction").GetDouble(), c.GetProperty("rtl").GetBoolean());
        var want = c.GetProperty("want");
        Assert.InRange(x, want[0].GetDouble() - 1e-9, want[0].GetDouble() + 1e-9);
        Assert.InRange(y, want[1].GetDouble() - 1e-9, want[1].GetDouble() + 1e-9);
    }

    [Theory, MemberData(nameof(Cases), "range_nudge")]
    public void Range_nudge(int i)
    {
        var c = Case("range_nudge", i);
        var step = c.GetProperty("step");
        var (key, n) = Forms.RangeNudge(c.GetProperty("current").GetDouble(), c.GetProperty("target").GetDouble(),
            step.ValueKind == JsonValueKind.Null ? null : step.GetDouble());
        var want = c.GetProperty("want");
        Assert.Equal(want[0].ValueKind == JsonValueKind.Null ? null : want[0].GetString(), key);
        Assert.Equal(want[1].GetInt32(), n);
    }

    [Theory, MemberData(nameof(Cases), "plan_datetime")]
    public void Plan_datetime(int i)
    {
        var c = Case("plan_datetime", i);
        var segs = c.GetProperty("segments").EnumerateArray().Select(s => s.GetString()!).ToList();
        var got = Forms.PlanDatetime(Str(c, "kind"), segs, Str(c, "value"), Str(c, "current"));
        var want = c.GetProperty("want");
        if (want.ValueKind == JsonValueKind.Null) { Assert.Null(got); return; }
        var expected = want.EnumerateArray().Select(a =>
            new DtAction(a[0].GetString()!, a[1].GetString()!, a.GetArrayLength() > 2 ? a[2].GetInt32() : null)).ToList();
        Assert.NotNull(got);
        Assert.Equal(expected, got!);
    }

    [Theory, MemberData(nameof(Cases), "datetime_equal")]
    public void Datetime_equal(int i)
    {
        var c = Case("datetime_equal", i);
        Assert.Equal(c.GetProperty("want").GetBoolean(), Forms.DatetimeEqual(Str(c, "kind"), Str(c, "a"), Str(c, "b")));
    }

    /// The vector's options object (Python/Node spelling) as Forms.NormalizeClickOptions arguments.
    private static NormalizedClick NormalizeFromJson(JsonElement o)
    {
        int? count = o.TryGetProperty("click_count", out var cc) ? cc.GetInt32()
                   : o.TryGetProperty("clickCount", out var cc2) ? cc2.GetInt32() : null;
        double? delay = o.TryGetProperty("delay", out var d) && d.ValueKind == JsonValueKind.Number ? d.GetDouble() : null;
        var mods = o.TryGetProperty("modifiers", out var m) ? m.EnumerateArray().Select(x => x.GetString()!).ToList() : null;
        (double, double)? pos = o.TryGetProperty("position", out var p) && p.ValueKind == JsonValueKind.Object
            ? (p.GetProperty("x").GetDouble(), p.GetProperty("y").GetDouble()) : null;
        return Forms.NormalizeClickOptions(Str(o, "button"), count, delay, mods, pos,
            o.TryGetProperty("force", out var f) && f.GetBoolean(), o.TryGetProperty("trial", out var t) && t.GetBoolean());
    }

    [Theory, MemberData(nameof(Cases), "normalize_click_options")]
    public void Normalize_click_options(int i)
    {
        var c = Case("normalize_click_options", i);
        var got = NormalizeFromJson(c.GetProperty("options"));
        var w = c.GetProperty("want");
        Assert.Equal(w.GetProperty("button").GetString(), got.Button);
        Assert.Equal(w.GetProperty("click_count").GetInt32(), got.ClickCount);
        Assert.Equal(w.GetProperty("modifiers").EnumerateArray().Select(x => x.GetString()!).ToArray(), got.Modifiers.ToArray());
        var delay = w.GetProperty("delay");
        double? wantDelay = delay.ValueKind == JsonValueKind.Null ? null : delay.GetDouble();
        Assert.Equal(wantDelay, got.Delay);
        var pos = w.GetProperty("position");
        (double X, double Y)? wantPos = pos.ValueKind == JsonValueKind.Null ? null : (pos.GetProperty("x").GetDouble(), pos.GetProperty("y").GetDouble());
        Assert.Equal(wantPos, got.Position);
        Assert.Equal(w.GetProperty("force").GetBoolean(), got.Force);
        Assert.Equal(w.GetProperty("trial").GetBoolean(), got.Trial);
    }

    [Theory, MemberData(nameof(Cases), "engine_path_ok")]
    public void Engine_path_ok(int i)
    {
        var c = Case("engine_path_ok", i);
        Assert.Equal(c.GetProperty("want").GetBoolean(), Forms.EnginePathOk(NormalizeFromJson(c.GetProperty("options"))));
    }

    [Fact]
    public void Human_click_options_map_onto_the_same_normalisation()
    {
        var o = new HumanClickOptions
        {
            Button = Microsoft.Playwright.MouseButton.Right, Delay = 80,
            Modifiers = new[] { Microsoft.Playwright.KeyboardModifier.ControlOrMeta, Microsoft.Playwright.KeyboardModifier.Shift,
                                Microsoft.Playwright.KeyboardModifier.Shift },
            Position = new Microsoft.Playwright.Position { X = 5, Y = 6 },
        }.Normalize();
        Assert.Equal("right", o.Button);
        Assert.Equal(1, o.ClickCount);
        Assert.Equal(new[] { "ControlOrMeta", "Shift" }, o.Modifiers.ToArray());
        Assert.Equal(80, o.Delay);
        Assert.Equal<(double, double)?>((5d, 6d), o.Position);
        Assert.Equal(2, new HumanClickOptions().Normalize(defaultCount: 2).ClickCount);
        Assert.Equal(3, new HumanClickOptions { ClickCount = 3 }.Normalize(defaultCount: 2).ClickCount);
        var from = HumanClickOptions.From(new Microsoft.Playwright.LocatorClickOptions { Trial = true, Force = true, ClickCount = 2, Timeout = 1500 });
        Assert.True(from.Trial && from.Force);
        Assert.Equal(2, from.ClickCount);
        Assert.Equal(1500, from.Timeout);
    }

    [Fact]
    public void Existing_click_calls_still_compile_and_human_options_convert()
    {
        // Source compatibility: these are the call shapes HumanClickAsync accepted before the forms port.
        // A second ILocator overload would make the target-typed `new()` one ambiguous (CS0121).
        Func<Microsoft.Playwright.ILocator, Task>[] calls =
        {
            l => l.HumanClickAsync(),
            l => l.HumanClickAsync(null),
            l => l.HumanClickAsync(new() { Timeout = 5 }),
            l => l.HumanClickAsync(new Microsoft.Playwright.LocatorClickOptions { Timeout = 5 }),
            l => l.HumanClickAsync(new HumanClickOptions { Button = Microsoft.Playwright.MouseButton.Right }),
        };
        Assert.Equal(5, calls.Length);
        Microsoft.Playwright.LocatorClickOptions converted = new HumanClickOptions
        {
            Button = Microsoft.Playwright.MouseButton.Middle, ClickCount = 2, Delay = 90, Force = true, Trial = true, Timeout = 7,
            Position = new Microsoft.Playwright.Position { X = 3, Y = 4 },
            Modifiers = new[] { Microsoft.Playwright.KeyboardModifier.Alt },
        };
        var back = HumanClickOptions.From(converted);
        Assert.Equal(Microsoft.Playwright.MouseButton.Middle, back.Button);
        Assert.Equal(2, back.ClickCount);
        Assert.Equal(90, back.Delay);
        Assert.True(back.Force && back.Trial);
        Assert.Equal(7, back.Timeout);
        Assert.Equal(3, back.Position!.X);
        Assert.Equal(new[] { Microsoft.Playwright.KeyboardModifier.Alt }, back.Modifiers);
    }

    [Fact]
    public void Typo_rate_comes_from_the_environment_at_call_time()
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_HUMANIZE_TYPO_RATE", null);
        Assert.Equal(Forms.DefaultTypoRate, Humanize.TypoRate());
        foreach (var (raw, want) in new (string, double)[] { ("0", 0), ("0.35", 0.35), (" 1 ", 1), ("7", 1), ("-2", 0), ("", 0.02), ("abc", 0.02), ("NaN", 0.02) })
        {
            sb.Env("CLEARCOTE_HUMANIZE_TYPO_RATE", raw);
            Assert.Equal(want, Humanize.TypoRate(), 9);
        }
    }

    [Fact]
    public async Task Forced_slips_are_all_corrected_when_the_field_cannot_be_read()
    {
        // A page whose isolated world cannot be reached (this fake has no CDP): every slip is corrected by
        // what it typed, so the text that reaches the field is still exactly the text asked for.
        var keyboard = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IKeyboard, HumanizeShiftTests.Recorder>();
        var mouse = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IMouse, HumanizeShiftTests.Recorder>();
        var page = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Keyboard = keyboard;
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        Humanize.RolloverOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(1.0));
        const string text = "slip 2024 ok";
        await page.HumanTypeAsync(text);
        var log = ((HumanizeShiftTests.Recorder)(object)keyboard).Log;
        var typed = new System.Text.StringBuilder();
        foreach (var (op, key) in log)
        {
            if (op != "press" && op != "down" && op != "type") continue;
            if (key == "Backspace") { if (typed.Length > 0) typed.Length--; }
            else if (key.Length == 1 && key != "Shift") typed.Append(key);
        }
        Assert.Equal(text, typed.ToString());
        Assert.Contains(log, e => e.Arg == "Backspace");   // at rate 1 every eligible character slipped
    }

    private static (Microsoft.Playwright.IPage Page, HumanizeShiftTests.Recorder Keyboard) RecordingPage()
    {
        var keyboard = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IKeyboard, HumanizeShiftTests.Recorder>();
        var mouse = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IMouse, HumanizeShiftTests.Recorder>();
        var page = System.Reflection.DispatchProxy.Create<Microsoft.Playwright.IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Keyboard = keyboard;
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        Humanize.RolloverOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        return (page, (HumanizeShiftTests.Recorder)(object)keyboard);
    }

    [Fact]
    public async Task The_typo_env_var_set_to_zero_turns_slips_off()
    {
        // The documented public opt-out (Humanize.TypoRateEnv): no per-page override, only the variable.
        using var sb = new Sandbox();
        sb.Env(Humanize.TypoRateEnv, "0");
        var (page, kb) = RecordingPage();
        await page.HumanTypeAsync("the quick brown fox 2024");
        Assert.DoesNotContain(kb.Log, e => e.Arg == "Backspace");
        Assert.Equal("the quick brown fox 2024", string.Concat(kb.Log.Where(e => e.Op == "press").Select(e => e.Arg)));
        sb.Env(Humanize.TypoRateEnv, "1");
        var (page2, kb2) = RecordingPage();
        await page2.HumanTypeAsync("the quick brown fox");
        Assert.Contains(kb2.Log, e => e.Arg == "Backspace");   // and the same variable turns them on
    }

    [Fact]
    public async Task Concurrent_calls_on_one_page_take_turns()
    {
        // Task.WhenAll of two humanized calls on ONE page: each one's keys arrive together, never
        // interleaved (they would type into each other's field).
        var (page, kb) = RecordingPage();
        Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        await Task.WhenAll(page.HumanTypeAsync("aaaaaa"), page.HumanTypeAsync("bbbbbb"), page.HumanPressAsync("Enter"));
        var typed = string.Concat(kb.Log.Where(e => e.Op == "press").Select(e => e.Arg == "Enter" ? "|" : e.Arg));
        Assert.Contains(typed, new[] { "aaaaaabbbbbb|", "bbbbbbaaaaaa|", "|aaaaaabbbbbb", "|bbbbbbaaaaaa", "aaaaaa|bbbbbb", "bbbbbb|aaaaaa" });
    }

    [Fact]
    public void Zero_or_negative_delay_is_the_persona_hold()
    {
        // delay=0 is Playwright's default ("no hold"); a negative one made Task.Delay(-1) wait forever with the
        // button down. Both mean "the persona's hold".
        foreach (var d in new float[] { 0, -1, -5, float.NaN, float.PositiveInfinity })
            Assert.Null(new HumanClickOptions { Delay = d }.Normalize().Delay);
        Assert.Equal(120.5, new HumanClickOptions { Delay = 120.5f }.Normalize().Delay);
    }

    // ------------------------------------------------------------------ properties beyond the vectors
    [Fact]
    public void Typos_never_touch_ineligible_characters()
    {
        var rng = new Random(1);
        foreach (var text in new[] { "ABC!@# ", "ÉÀü", "--__..", "\t\n" })
        {
            var chars = Forms.Chars(text);
            for (int i = 0; i < chars.Length; i++)
                for (int k = 0; k < 200; k++)
                    Assert.Null(Forms.PlanTypo(chars, i, rng.NextDouble, rate: 1.0));
        }
    }

    [Fact]
    public void Typo_wrong_key_is_a_real_neighbour_of_the_same_class()
    {
        var rng = new Random(2);
        var text = Forms.Chars("abcdefghijklmnopqrstuvwxyz0123456789");
        var seen = new HashSet<string>();
        for (int n = 0; n < 3000; n++)
        {
            int i = rng.Next(text.Length);
            var tp = Forms.PlanTypo(text, i, rng.NextDouble, rate: 1.0);
            Assert.NotNull(tp);
            seen.Add(tp!.Kind);
            if (tp.Kind == "sub")
            {
                var ch = text[i];
                var w = tp.Wrong!;
                Assert.NotEqual(ch, w);
                Assert.Equal(char.IsDigit(ch[0]), char.IsDigit(w[0]));
                Assert.Contains(w, char.IsDigit(ch[0]) ? Forms.NearbyDigits[ch] : Forms.NearbyLetters[ch]);
                foreach (var c in text.Skip(i + 1).Take(tp.Late)) Assert.True(Forms.TypoEligible(c) || c == " ");
                Assert.InRange(tp.Late, 0, 2);
            }
            if (tp.Kind == "swap")
                Assert.True(i + 1 < text.Length && text[i + 1] != text[i] && Forms.TypoEligible(text[i + 1]));
        }
        Assert.Equal(new HashSet<string> { "sub", "double", "swap" }, seen);
    }

    [Fact]
    public void Typo_rate_is_roughly_honoured()
    {
        var rng = new Random(3);
        var text = Forms.Chars("hello world");
        int hits = Enumerable.Range(0, 20000).Count(_ => Forms.PlanTypo(text, 1, rng.NextDouble, rate: 0.02) is not null);
        Assert.InRange(hits, 250, 550);   // 2 % of 20000 = 400
    }

    [Fact]
    public void Zero_rate_means_no_typos()
    {
        var rng = new Random(4);
        Assert.All(Enumerable.Range(0, 1000), _ => Assert.Null(Forms.PlanTypo("abc", 0, rng.NextDouble, rate: 0.0)));
    }

    [Fact]
    public void Letters_and_digits_neighbour_maps_never_map_to_themselves()
    {
        foreach (var (k, v) in Forms.NearbyLetters.Concat(Forms.NearbyDigits)) Assert.DoesNotContain(k, v);
    }

    /// An independent restatement of type_ahead.cc, to fuzz the simulator against.
    private static int BlinkReference(IReadOnlyList<string?> labels, int start, string keys)
    {
        int n = labels.Count;
        if (n == 0) return start;
        string buf = "";
        string? rep = null;
        int cur = start;
        foreach (var k in keys.ToLowerInvariant())
        {
            var c = k.ToString();
            buf += c;
            string prefix;
            int off;
            if (c == rep) { prefix = c; off = 1; }
            else if (buf.Length > 1) { prefix = buf; off = 0; rep = null; }
            else { prefix = buf; off = 1; rep = c; }
            int i0 = ((cur >= 0 ? cur : 0) + off) % n;
            for (int j = 0; j < n; j++)
            {
                int x = (i0 + j) % n;
                var lab = labels[x];
                if (!string.IsNullOrEmpty(lab) && lab.TrimStart().ToLowerInvariant().StartsWith(prefix, StringComparison.Ordinal))
                {
                    cur = x;
                    break;
                }
            }
        }
        return cur;
    }

    [Fact]
    public void Typeahead_matches_an_independent_restatement()
    {
        var rng = new Random(5);
        const string alphabet = "aabbcd";
        for (int n = 0; n < 3000; n++)
        {
            var labels = Enumerable.Range(0, rng.Next(1, 9)).Select(_ =>
            {
                var s = new string(Enumerable.Range(0, rng.Next(0, 5)).Select(_ => alphabet[rng.Next(alphabet.Length)]).ToArray());
                return s.Length == 0 ? null : s;
            }).ToList();
            int start = rng.Next(-1, labels.Count);
            var keys = new string(Enumerable.Range(0, rng.Next(1, 5)).Select(_ => "abcd"[rng.Next(4)]).ToArray());
            Assert.Equal(BlinkReference(labels, start, keys), Forms.TypeaheadSimulate(labels, start, keys));
        }
    }

    [Fact]
    public void Plan_select_always_lands_on_the_target()
    {
        // Whatever the plan, simulating it (type-ahead, then arrows over enabled rows) must end on the
        // target -- the property the live Enter relies on.
        var rng = new Random(6);
        var words = new[] { "alpha", "alpine", "beta", "bet", "gamma", "Gamma", "delta", "  delta", "zeta", "", "eta",
                            "eta", "Ünï", "item 1", "item 2", "x", "xx", "xxx" };
        for (int n = 0; n < 4000; n++)
        {
            var labels = Enumerable.Range(0, rng.Next(1, 13))
                .Select(_ => rng.NextDouble() > 0.15 ? words[rng.Next(words.Length)] : null).ToList();
            var enabled = Enumerable.Range(0, labels.Count).Where(i => labels[i] is not null).ToList();
            if (enabled.Count == 0) continue;
            int target = enabled[rng.Next(enabled.Count)];
            // any current row: an enabled one, none (-1), or a DISABLED one (a page can preselect it)
            var choices = enabled.Append(-1).Concat(Enumerable.Range(0, labels.Count).Where(i => labels[i] is null)).ToList();
            int current = choices[rng.Next(choices.Count)];
            var plan = Forms.PlanSelect(labels, current, target);
            Assert.NotNull(plan);
            int land = plan!.Keys.Length > 0 ? Forms.TypeaheadSimulate(labels, current >= 0 ? current : -1, plan.Keys)
                                             : (current >= 0 ? current : enabled[0]);
            int pos = land, step = plan.Arrows > 0 ? 1 : -1;
            for (int k = 0; k < Math.Abs(plan.Arrows); k++)
            {
                pos += step;
                while (labels[pos] is null) pos += step;
            }
            Assert.True(pos == target, $"labels={string.Join("|", labels)} current={current} target={target} plan={plan}");
            Assert.All(plan.Keys, ch => Assert.Contains(ch, Forms.Typeable));
        }
    }

    [Fact]
    public void Plan_select_is_never_worse_than_arrows_alone()
    {
        var rng = new Random(7);
        for (int n = 0; n < 2000; n++)
        {
            var labels = Enumerable.Range(0, rng.Next(2, 31)).Select(_ => (string?)("opt" + rng.Next(0, 31))).ToList();
            int cur = rng.Next(labels.Count), tgt = rng.Next(labels.Count);
            var plan = Forms.PlanSelect(labels, cur, tgt)!;
            Assert.True(plan.Keys.Length + Math.Abs(plan.Arrows) <= Math.Abs(tgt - cur));
        }
    }

    private static string R(double v) => v.ToString("R", CultureInfo.InvariantCulture);

    [Fact]
    public void Range_sanitize_lands_on_the_step_grid_inside_the_bounds()
    {
        var rng = new Random(8);
        double[] los = { 0, -10, 3, 0.5 }, spans = { 1, 7, 100, 250.5 }, steps = { 1, 2, 5, 0.1, 0.25, 7 };
        for (int n = 0; n < 3000; n++)
        {
            double lo = los[rng.Next(los.Length)], hi = lo + spans[rng.Next(spans.Length)], step = steps[rng.Next(steps.Length)];
            double asked = lo - 20 + rng.NextDouble() * (hi - lo + 40);
            double v = Forms.RangeSanitize(R(asked), R(lo), R(hi), R(step));
            Assert.InRange(v, lo - 1e-9, hi + 1e-9);
            double k = (v - lo) / step;
            Assert.True(Math.Abs(k - Math.Round(k)) < 1e-6, $"{v} is off the {step} grid from {lo}");
        }
    }

    [Fact]
    public void Range_point_stays_on_the_track()
    {
        var rng = new Random(9);
        for (int n = 0; n < 1000; n++)
        {
            var box = new Box(rng.NextDouble() * 500, rng.NextDouble() * 500, 1 + rng.NextDouble() * 599, 16);
            var (x, y) = Forms.RangePoint(box, rng.NextDouble(), rng.NextDouble() < 0.5);
            Assert.InRange(x, box.X, box.X + box.Width);
            Assert.Equal(box.Y + 8, y);
        }
    }

    [Fact]
    public void Every_datetime_plan_types_only_digits_and_ampm_letters()
    {
        var kinds = new Dictionary<string, (string[] Segs, string Value)>
        {
            ["date"] = (new[] { "month-field", "day-field", "year-field" }, "2031-02-28"),
            ["time"] = (new[] { "hour-field", "minute-field", "ampm-field" }, "23:59"),
            ["datetime-local"] = (new[] { "month-field", "day-field", "year-field", "hour-field", "minute-field", "ampm-field" }, "1999-12-31T00:00"),
            ["week"] = (new[] { "week-field", "year-field" }, "2020-W01"),
        };
        foreach (var (kind, (segs, value)) in kinds)
        {
            var plan = Forms.PlanDatetime(kind, segs, value);
            Assert.NotNull(plan);
            Assert.NotEmpty(plan!);
            foreach (var a in plan)
            {
                if (a.Op == "type") Assert.True(a.Arg.All(char.IsAsciiDigit) || a.Arg is "a" or "p", a.ToString());
                else Assert.Contains(a.Arg, new[] { "ArrowRight", "ArrowUp", "ArrowDown" });
            }
        }
    }

    [Fact]
    public void Segment_names_and_nodes_from_a_pierced_describe_tree()
    {
        static object El(string? pseudo = null, int? bid = null, object[]? children = null, object[]? shadow = null)
        {
            var n = new Dictionary<string, object?>
            {
                ["nodeName"] = "DIV", ["attributes"] = pseudo is null ? Array.Empty<string>() : new[] { "pseudo", pseudo },
                ["backendNodeId"] = bid,
            };
            if (children is not null) n["children"] = children;
            if (shadow is not null) n["shadowRoots"] = shadow;
            return n;
        }
        var tree = El(bid: 1, shadow: new[] { El(bid: 2, children: new[] {
            El("-webkit-datetime-edit", 3, children: new[] {
                El("-webkit-datetime-edit-month-field", 4), El("-webkit-datetime-edit-text", 5),
                El("-webkit-datetime-edit-day-field", 6), El("-webkit-datetime-edit-text", 7),
                El("-webkit-datetime-edit-year-field", 8) }),
            El("-webkit-calendar-picker-indicator", 9) }) });
        var json = JsonDocument.Parse(JsonSerializer.Serialize(tree)).RootElement;
        Assert.Equal(new[] { "month-field", "day-field", "year-field" }, Forms.SegmentNamesFromTree(json));
        Assert.Equal(new (string, int?)[] { ("month-field", 4), ("day-field", 6), ("year-field", 8) }, Forms.SegmentNodesFromTree(json));
    }

    // ------------------------------------------------------------------ the CDP side (IsolatedWorld)
    [Fact]
    public void Node_at_hit_tests_in_document_coordinates()
    {
        // Measured on r37: viewport coordinates found nothing once the page was scrolled.
        var p = IsolatedWorld.NodeAtParams(100.4, 50.6, (0, 547));
        Assert.Equal(100, (int)p["x"]);
        Assert.Equal(598, (int)p["y"]);
        Assert.False((bool)p["includeUserAgentShadowDOM"]);
        Assert.Equal(12, (int)IsolatedWorld.NodeAtParams(12.5, 0, null)["x"]);   // round half to even, like Python
    }

    [Fact]
    public void Node_at_skips_pointer_events_none_layers_like_a_real_press()
    {
        // ignorePointerEventsNone = true makes CDP hit-test pointer-events:none elements TOO: a decorative layer
        // over a control answered instead of the control and every form route fell back to stock Playwright
        // (SC 209). The flag must be absent or false.
        var p = IsolatedWorld.NodeAtParams(5, 5, null);
        Assert.False(p.TryGetValue("ignorePointerEventsNone", out var v) && (bool)v);
    }

    [Fact]
    public void A_child_frame_point_is_placed_under_the_main_documents_iframe()
    {
        // A point inside a child frame is answered by the <iframe> in the MAIN document that holds it (through
        // every nesting level): DOM.getFrameOwner of the frame directly under the main one.
        var tree = JsonDocument.Parse(@"{""frame"":{""id"":""MAIN""},""childFrames"":[
            {""frame"":{""id"":""C1"",""parentId"":""MAIN""},""childFrames"":[{""frame"":{""id"":""C2"",""parentId"":""C1""}}]},
            {""frame"":{""id"":""D1"",""parentId"":""MAIN""}}]}").RootElement;
        Assert.Equal("C1", IsolatedWorld.TopChildFrame(tree, "C1", "MAIN"));
        Assert.Equal("C1", IsolatedWorld.TopChildFrame(tree, "C2", "MAIN"));
        Assert.Equal("D1", IsolatedWorld.TopChildFrame(tree, "D1", "MAIN"));
        Assert.Null(IsolatedWorld.TopChildFrame(tree, "MAIN", "MAIN"));
        Assert.Null(IsolatedWorld.TopChildFrame(tree, "GONE", "MAIN"));
    }

    [Fact]
    public void The_read_sources_are_the_python_ones()
    {
        // Copied verbatim from sdk/python/clearcote/_isolated.py; a drift here is a drift in what a page
        // could observe, so pin the parts that matter.
        Assert.Equal("() => [scrollX, scrollY]", IsolatedWorld.Scroll);
        Assert.Contains("e.isContentEditable ? e.textContent : null", IsolatedWorld.ActiveValue);
        Assert.Contains("autocomplete: e.getAttribute('autocomplete')", IsolatedWorld.ActiveField);
        Assert.StartsWith("function (a) {", IsolatedWorld.Describe);
        Assert.Contains("bl: e.clientLeft || 0, bt: e.clientTop || 0", IsolatedWorld.Describe);
        Assert.Contains("o.optRects", IsolatedWorld.Describe);
        Assert.Equal("input, textarea, select, [contenteditable=''], [contenteditable='true'], [contenteditable='plaintext-only']",
            IsolatedWorld.FormControl);
        // With the Python SDK checked out beside this one, compare the sources byte for byte.
        var pyPath = Path.Combine(Path.GetDirectoryName(SharedTestData.PathOf("humanize_forms_vectors.json"))!,
            "..", "python", "clearcote", "_isolated.py");
        if (!File.Exists(pyPath)) return;
        var py = File.ReadAllText(pyPath).Replace("\r\n", "\n");
        foreach (var src in new[] { IsolatedWorld.ActiveValue, IsolatedWorld.ActiveField, IsolatedWorld.Describe, IsolatedWorld.FormControl,
                                    IsolatedWorld.HitInBox, IsolatedWorld.ListboxRow, IsolatedWorld.CssBox, IsolatedWorld.ScrollBy })
            Assert.Contains(src.Replace("\r\n", "\n"), py);
    }
}
