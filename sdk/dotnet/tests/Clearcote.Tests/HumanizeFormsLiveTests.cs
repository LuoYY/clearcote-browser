using System.Diagnostics;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// One Clearcote browser for the live forms suite (a persistent context in a temp profile that is removed
/// afterwards). Nothing is launched unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
///
/// TMPDIR is left as it was found (SDK CI fails on leftovers): the profile dir is removed, and so is the
/// shared fontconfig cache a Linux launch creates (Fonts.cs) when this suite was the one that made it.
public sealed class FormsLiveBrowser : IAsyncLifetime
{
    public static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
    public IBrowserContext? Context { get; private set; }
    private string? _dir;
    private static readonly string FcCache = Path.Join(Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar), "cc-fc-cache");
    private bool _fcCacheExisted = true;

    public async Task InitializeAsync()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        _fcCacheExisted = Directory.Exists(FcCache);
        _dir = TestTemp.Create("cc-live-forms-");
        Context = await Clearcote.LaunchPersistentContextAsync(_dir, new LaunchOptions
        {
            ExecutablePath = LiveExe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true,
            Fingerprint = "forms-live-1",
        });
    }

    public async Task DisposeAsync()
    {
        try
        {
            if (Context is not null) await Context.CloseAsync();
        }
        finally
        {
            if (_dir is not null) TestTemp.Remove(_dir);
            if (!_fcCacheExisted) TestTemp.Remove(FcCache);
        }
    }
}

/// LIVE humanized form input against a real Clearcote build (sdk/testdata/humanize_forms.html), mirroring
/// sdk/python/tests/test_humanize_forms_live.py with the .NET explicit API. Skipped (a no-op, as in
/// HumanizeLiveTests) unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary; add CLEARCOTE_LICENSE_KEY for
/// a PRO build. Every assertion is about what a PAGE sees: trusted events, key holds, how many change
/// events a selection fires, and the exact value left in the field -- the things a behavioural detector and
/// a form backend read.
///
/// Not ported: the two raw mouse.click(x, y) / mouse.dblclick(x, y) checks. .NET humanize is explicit
/// extension methods and has no humanized IMouse surface for them to exercise.
public class HumanizeFormsLiveTests : IClassFixture<FormsLiveBrowser>
{
    private const double MinHoldMs = 25;   // a finger cannot hold a key or button shorter than this; persona floor is 45
    private readonly FormsLiveBrowser _b;

    public HumanizeFormsLiveTests(FormsLiveBrowser b) => _b = b;

    private static readonly string Fixture = new Uri(SharedTestData.PathOf("humanize_forms.html")).AbsoluteUri;

    /// A fresh page on the fixture, or null when no live engine is configured. The test closes it.
    private async Task<IPage?> PageAsync()
    {
        if (_b.Context is null) return null;
        var page = await _b.Context.NewPageAsync();
        Humanize.Attach(page, "forms-live-1");
        await page.GotoAsync(Fixture);
        return page;
    }

    // Why a humanized route handed an action to Playwright during the current test (printed by the
    // assertions that catch a route falling back where it should not). Tests run serially.
    private static readonly List<string> Fallbacks = new();
    private static string Why => Fallbacks.Count == 0 ? "" : " [fallbacks: " + string.Join("; ", Fallbacks) + "]";

    private async Task Live(Func<IPage, Task> body)
    {
        var page = await PageAsync();
        if (page is null) return;
        Fallbacks.Clear();
        Humanize.FallbackTrace = s => { lock (Fallbacks) Fallbacks.Add(s); };
        try
        {
            await body(page);
        }
        finally
        {
            Humanize.FallbackTrace = null;
            await page.CloseAsync();
        }
    }

    /// CLEARCOTE_HUMANIZE_TYPO_RATE for one test (read at call time; restored afterwards). Tests run
    /// serially in this assembly, so the variable cannot leak into another test.
    private static async Task WithTypoRate(double rate, Func<Task> body)
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_HUMANIZE_TYPO_RATE", rate.ToString(System.Globalization.CultureInfo.InvariantCulture));
        await body();
    }

    // ------------------------------------------------------------------------- event helpers
    private static async Task<List<JsonElement>> Ev(IPage page, string? t = null, string? id = null)
    {
        var all = await page.EvaluateAsync<JsonElement>("window.__ev");
        return all.EnumerateArray()
            .Where(e => (t is null || S(e, "t") == t) && (id is null || S(e, "id") == id))
            .Select(e => e.Clone()).ToList();
    }

    private static string S(JsonElement e, string n) => e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString()! : "";
    private static double D(JsonElement e, string n) => e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : double.NaN;
    private static bool Tr(JsonElement e) => e.GetProperty("tr").GetBoolean();

    private static Task Reset(IPage page) => page.EvaluateAsync("window.__ev.length = 0");

    /// (key, keydown->keyup ms) for every key that went to element <paramref name="id"/>.
    private static async Task<List<(string Key, double Ms)>> KeyHolds(IPage page, string? id = null)
    {
        var open = new Dictionary<string, double>();
        var output = new List<(string, double)>();
        foreach (var e in await Ev(page))
        {
            var t = S(e, "t");
            if ((t != "keydown" && t != "keyup") || (id is not null && S(e, "id") != id)) continue;
            var k = S(e, "code").Length > 0 ? S(e, "code") : S(e, "key");
            if (t == "keydown") open.TryAdd(k, D(e, "ts"));
            else if (open.Remove(k, out var t0)) output.Add((S(e, "key"), D(e, "ts") - t0));
        }
        return output;
    }

    private static async Task AssertHumanKeys(IPage page, string id)
    {
        var holds = await KeyHolds(page, id);
        Assert.True(holds.Count > 0, $"no keys reached #{id}{Why}");
        var shortHolds = holds.Where(h => h.Ms < MinHoldMs).ToList();
        Assert.True(shortHolds.Count == 0, "keys held shorter than a finger can: " + string.Join(", ", shortHolds));
        Assert.All((await Ev(page)).Where(e => S(e, "t") is "keydown" or "keyup" or "input" or "change" && S(e, "id") == id),
            e => Assert.True(Tr(e)));
    }

    /// The press came the humanized way: the pointer travelled there, and every press on <paramref name="id"/>
    /// was held like a finger holds it (Playwright's own click teleports and holds ~0 ms). These extra
    /// checks are what fail when a route silently falls back to the native call.
    private static async Task AssertHumanPress(IPage page, string id)
    {
        // >= 3, not Glide_precedes_the_press's 5: the renderer coalesces moves that arrive faster than a
        // frame, so a short glide can deliver few events; Playwright's own click delivers one.
        var all = await Ev(page);
        Assert.True(all.Where(e => S(e, "t") == "mousemove").Sum(m => D(m, "n")) >= 3,
            "the pointer travels to the target" + Why + ": "
            + string.Join(" ", all.Select(e => $"{S(e, "t")}:{S(e, "id")}:{D(e, "n")}@{D(e, "ts"):F0}")));
        var downs = await Ev(page, "mousedown", id);
        var ups = await Ev(page, "mouseup", id);
        Assert.NotEmpty(downs);
        Assert.Equal(downs.Count, ups.Count);
        for (int i = 0; i < downs.Count; i++)
            Assert.True(D(ups[i], "ts") - D(downs[i], "ts") >= 40, $"press {i} on #{id} held like a finger{Why}");
        Assert.True(Fallbacks.Count == 0, "no humanized route fell back to Playwright" + Why);
    }

    /// One trusted change, from a drop-down opened with the mouse (Playwright's SelectOptionAsync fires an
    /// untrusted one with no pointer at all).
    private static async Task AssertOneTrustedChange(IPage page, string id)
    {
        var ch = await Changes(page, id);
        Assert.True(ch.Count == 1, $"{ch.Count} change events{Why}");
        Assert.True(Tr(ch[0]), "the change was fired by the engine, not by script" + Why);
        Assert.NotEmpty(await Ev(page, "mousedown", id));
        Assert.True(Fallbacks.Count == 0, "no humanized route fell back to Playwright" + Why);
    }

    private static async Task<List<JsonElement>> Pressed(IPage page, string key, string? id = null)
        => (await Ev(page, "keydown", id)).Where(e => S(e, "key") == key).ToList();

    private static async Task<List<JsonElement>> Changes(IPage page, string id) => await Ev(page, "change", id);

    // ------------------------------------------------------------------------------------- clicks
    [Fact]
    public Task Right_click_is_a_right_click() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { Button = MouseButton.Right });
        var downs = await Ev(page, "mousedown", "btn");
        Assert.Equal(new[] { 2.0 }, downs.Select(d => D(d, "button")).ToArray());
        Assert.True(Tr(downs[0]));
        Assert.True(D((await Ev(page, "mouseup", "btn"))[0], "ts") - D(downs[0], "ts") >= 40, "held like a finger");
        Assert.NotEmpty(await Ev(page, "contextmenu", "btn"));   // a right click opens the context menu
        Assert.Empty(await Ev(page, "click", "btn"));            // a right click is not a click
    });

    [Fact]
    public Task Middle_click_through_a_locator() => Live(async page =>
    {
        await Reset(page);
        await page.Locator("#btn").HumanClickAsync(new HumanClickOptions { Button = MouseButton.Middle });
        Assert.Equal(new[] { 1.0 }, (await Ev(page, "mousedown", "btn")).Select(d => D(d, "button")).ToArray());
        Assert.NotEmpty(await Ev(page, "auxclick", "btn"));
        await AssertHumanPress(page, "btn");
    });

    [Fact]
    public Task Click_count_two_is_a_double_click() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { ClickCount = 2 });
        Assert.Equal(new[] { 1.0, 2.0 }, (await Ev(page, "mousedown", "btn")).Select(d => D(d, "detail")).ToArray());
        Assert.True(Tr((await Ev(page, "dblclick", "btn"))[0]));
        await AssertHumanPress(page, "btn");
    });

    [Fact]
    public Task Dblclick_has_human_holds_and_gap() => Live(async page =>
    {
        await Reset(page);
        await page.HumanDblClickAsync("#btn");
        var downs = await Ev(page, "mousedown", "btn");
        var ups = await Ev(page, "mouseup", "btn");
        Assert.Equal(2, downs.Count);
        Assert.Equal(2, ups.Count);
        for (int i = 0; i < 2; i++) Assert.True(D(ups[i], "ts") - D(downs[i], "ts") >= 40, "button held like a finger");
        var gap = D(downs[1], "ts") - D(ups[0], "ts");
        Assert.InRange(gap, 50, 450);
        Assert.NotEmpty(await Ev(page, "dblclick", "btn"));
    });

    [Fact]
    public Task Modifiers_are_held_around_the_press() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { Modifiers = new[] { KeyboardModifier.Shift } });
        var click = (await Ev(page, "click", "btn"))[0];
        Assert.True(click.GetProperty("shift").GetBoolean() && Tr(click));
        var shiftDown = D((await Pressed(page, "Shift"))[0], "ts");
        var shiftUp = D((await Ev(page, "keyup")).First(e => S(e, "key") == "Shift"), "ts");
        Assert.True(shiftDown < D((await Ev(page, "mousedown", "btn"))[0], "ts"));
        Assert.True(shiftUp > D((await Ev(page, "mouseup", "btn"))[0], "ts"));
        await AssertHumanPress(page, "btn");
    });

    [Fact]
    public Task Position_is_honoured() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { Position = new Position { X = 10, Y = 12 } });
        var d = (await Ev(page, "mousedown", "btn"))[0];
        Assert.InRange(D(d, "ox"), 10 - 1.5, 10 + 1.5);
        Assert.InRange(D(d, "oy"), 12 - 1.5, 12 + 1.5);
        await AssertHumanPress(page, "btn");
    });

    [Fact]
    public Task Trial_performs_no_action() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#trialbtn", new HumanClickOptions { Trial = true });
        await page.Locator("#trialbtn").HumanClickAsync(new HumanClickOptions { Trial = true });
        await page.Locator("#trialbtn").HumanClickAsync(new LocatorClickOptions { Trial = true });
        await page.HumanHoverAsync("#trialbtn", new HumanClickOptions { Trial = true });
        await page.HumanCheckAsync("#cb", new PageCheckOptions { Trial = true });
        await page.Locator("#cb").HumanCheckAsync(new LocatorCheckOptions { Trial = true });
        Assert.Empty(await Ev(page, "mousedown", "trialbtn"));
        Assert.Empty(await Ev(page, "click", "trialbtn"));
        Assert.False(await page.IsCheckedAsync("#cb"));
        Assert.Empty(await Ev(page, "click", "cb"));
        Assert.Empty(await Ev(page, "mousedown", "cb"));
    });

    [Fact]
    public Task Delay_sets_the_hold() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { Delay = 300 });
        var d = (await Ev(page, "mousedown", "btn"))[0];
        var u = (await Ev(page, "mouseup", "btn"))[0];
        Assert.InRange(D(u, "ts") - D(d, "ts"), 270, 450);
        await AssertHumanPress(page, "btn");
    });

    [Fact]
    public Task Glide_precedes_the_press() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn");
        var moves = (await Ev(page)).Where(e => S(e, "t") == "mousemove");
        Assert.True(moves.Sum(m => D(m, "n")) >= 5, "the pointer travels to the target");
    });

    [Fact]
    public Task Hover_glides_without_pressing() => Live(async page =>
    {
        await Reset(page);
        await page.HumanHoverAsync("#btn", new HumanClickOptions { Modifiers = new[] { KeyboardModifier.Shift } });
        Assert.Empty(await Ev(page, "mousedown"));
        Assert.True((await Ev(page)).Where(e => S(e, "t") == "mousemove").Sum(m => D(m, "n")) >= 5);
        Assert.NotEmpty(await Pressed(page, "Shift"));   // modifiers held across the move
    });

    [Fact]
    public Task Locator_strictness_still_raises() => Live(async page =>
    {
        await Assert.ThrowsAnyAsync<PlaywrightException>(
            () => page.Locator("button.dup").HumanClickAsync(new HumanClickOptions { Timeout = 2000 }));
        await Assert.ThrowsAnyAsync<PlaywrightException>(
            () => page.Locator("button.dup").HumanDblClickAsync(new HumanClickOptions { Timeout = 2000 }));
    });

    [Fact]
    public Task Missing_element_fails_after_one_timeout() => Live(async page =>
    {
        foreach (var act in new Func<Task>[]
        {
            () => page.HumanClickAsync("#nope", new HumanClickOptions { Timeout = 1500 }),
            () => page.HumanFillAsync("#nope", "x", new PageFillOptions { Timeout = 1500 }),
            () => page.Locator("#nope").HumanClickAsync(new HumanClickOptions { Timeout = 1500 }),
            () => page.Locator("#nope").HumanFillAsync("x", new LocatorFillOptions { Timeout = 1500 }),
            () => page.HumanSelectOptionAsync("#nope", new[] { "x" }, new PageSelectOptionOptions { Timeout = 1500 }),
            () => page.HumanCheckAsync("#nope", new PageCheckOptions { Timeout = 1500 }),
        })
        {
            var sw = Stopwatch.StartNew();
            await Assert.ThrowsAnyAsync<System.TimeoutException>(act);
            Assert.True(sw.Elapsed.TotalSeconds < 2.8, $"a missing element must not wait the timeout twice ({sw.Elapsed.TotalSeconds:F2}s)");
        }
    });

    [Fact]
    public Task Check_uncheck_and_set_checked_are_humanized() => Live(async page =>
    {
        await Reset(page);
        await page.HumanCheckAsync("#cb");
        Assert.True(await page.IsCheckedAsync("#cb"));
        Assert.NotEmpty(await Ev(page, "mousedown", "cb"));
        Assert.All(await Ev(page, "click", "cb"), e => Assert.True(Tr(e)));
        // held like a finger (Playwright's own CheckAsync presses for ~0 ms)
        Assert.True(D((await Ev(page, "mouseup", "cb"))[0], "ts") - D((await Ev(page, "mousedown", "cb"))[0], "ts") >= 40);
        await page.Locator("#cb").HumanSetCheckedAsync(false);
        Assert.False(await page.IsCheckedAsync("#cb"));
        await page.HumanUncheckAsync("#cb");   // already unchecked: nothing happens
        Assert.False(await page.IsCheckedAsync("#cb"));
        Assert.Equal(2, (await Ev(page, "click", "cb")).Count);
        await page.Locator("#rb").HumanCheckAsync();
        Assert.True(await page.IsCheckedAsync("#rb"));
        Assert.All(await Ev(page, "click", "rb"), e => Assert.True(Tr(e)));
    });

    // ------------------------------------------------------------------------------------- text
    [Fact]
    public Task Fill_text_with_held_keys() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#name", "Ada Lovelace");
        Assert.Equal("Ada Lovelace", await page.InputValueAsync("#name"));
        await AssertHumanKeys(page, "name");
    });

    [Fact]
    public Task Fill_clears_a_prefilled_field_with_held_keys() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#prefilled", "new");
        Assert.Equal("new", await page.InputValueAsync("#prefilled"));
        var holds = new Dictionary<string, double>();
        foreach (var (k, d) in await KeyHolds(page, "prefilled"))
            if (k is "Control" or "Meta" or "a" or "Backspace") holds[k] = d;
        Assert.True(holds.ContainsKey("Backspace") && holds["Backspace"] >= MinHoldMs);
        Assert.True(holds.ContainsKey("Control") || holds.ContainsKey("Meta"));
        Assert.All(holds.Values, d => Assert.True(d >= MinHoldMs, string.Join(", ", holds)));
    });

    [Fact]
    public Task Fill_on_an_empty_field_presses_no_clear_keys() => Live(page => WithTypoRate(0, async () =>
    {
        await Reset(page);
        await page.HumanFillAsync("#bio", "hello");
        Assert.Equal("hello", await page.InputValueAsync("#bio"));
        await AssertHumanKeys(page, "bio");
        Assert.Empty((await Ev(page, "keydown", "bio")).Where(e => S(e, "key") is "Backspace" or "Control" or "Meta"));
    }));

    [Fact]
    public Task Fill_with_an_empty_string_empties_the_field() => Live(async page =>
    {
        await page.HumanFillAsync("#prefilled", "abc");
        await Reset(page);
        await page.Locator("#prefilled").HumanFillAsync("");
        Assert.Equal("", await page.InputValueAsync("#prefilled"));
        await AssertHumanKeys(page, "prefilled");
    });

    [Fact]
    public Task Get_by_label_and_role_are_humanized() => Live(async page =>
    {
        await Reset(page);
        await page.GetByLabel("City").HumanFillAsync("Paris");
        Assert.Equal("Paris", await page.InputValueAsync("#city"));
        await AssertHumanKeys(page, "city");
        await Reset(page);
        await page.GetByRole(AriaRole.Textbox, new PageGetByRoleOptions { Name = "City" }).HumanTypeAsync("!");
        Assert.Equal("Paris!", await page.InputValueAsync("#city"));
        await AssertHumanKeys(page, "city");
    });

    [Fact]
    public Task Filter_and_first_locators_are_humanized() => Live(async page =>
    {
        await Reset(page);
        await page.Locator("input[type=text]").Filter(new LocatorFilterOptions { HasNotText = "zzz" }).Nth(0).HumanFillAsync("first");
        Assert.Equal("first", await page.InputValueAsync("#name"));
        await AssertHumanKeys(page, "name");
    });

    [Fact]
    public Task Field_under_a_floating_label() => Live(async page =>
    {
        await Reset(page);
        // The label covers every probe point of the field: a person clicks the label (it passes the focus on)
        // and types; the fill must not fall back to Playwright's insertText. (Typo rate 0: the exact keys
        // are asserted.)
        await WithTypoRate(0, async () => await page.HumanFillAsync("#float", "abc"));
        Assert.Equal("abc", await page.InputValueAsync("#float"));
        Assert.Equal(new[] { "a", "b", "c" }, (await Ev(page, "keydown", "float")).Select(e => S(e, "key")).ToArray());
        await AssertHumanKeys(page, "float");
    });

    [Fact]
    public Task Textarea_newlines_and_contenteditable() => Live(async page =>
    {
        await page.HumanFillAsync("#bio", "two\nlines");
        Assert.Equal("two\nlines", await page.InputValueAsync("#bio"));
        await page.HumanFillAsync("#ce", "hello world");
        Assert.Equal("hello world", await page.EvalOnSelectorAsync<string>("#ce", "e => e.textContent"));
        await AssertHumanKeys(page, "bio");
        await AssertHumanKeys(page, "ce");
    });

    [Fact]
    public Task Press_holds_the_key() => Live(async page =>
    {
        await page.HumanFillAsync("#name", "x");
        await Reset(page);
        await page.Locator("#name").HumanPressAsync("Backspace");
        Assert.Equal("", await page.InputValueAsync("#name"));
        await AssertHumanKeys(page, "name");
    });

    // ------------------------------------------------------------------------------------- typos
    [Fact]
    public Task Forced_typos_are_corrected() => Live(page => WithTypoRate(0.35, async () =>
    {
        await Reset(page);
        const string text = "the quick brown fox jumps over 2024";
        await page.HumanFillAsync("#name", text);
        Assert.Equal(text, await page.InputValueAsync("#name"));
        Assert.NotEmpty(await Pressed(page, "Backspace", "name"));   // with a 35 % rate some slip had to be corrected
        await AssertHumanKeys(page, "name");
    }));

    [Theory]
    [InlineData("#email", "ada@example.com")]
    [InlineData("#pw", "s3cretPassw0rd")]
    [InlineData("#num", "123456")]
    [InlineData("#tel", "5551234567")]
    [InlineData("#url", "https://example.com/a")]
    [InlineData("#otp", "123456")]
    public Task No_typos_where_a_slip_breaks_the_field(string sel, string value) => Live(page => WithTypoRate(0.6, async () =>
    {
        await Reset(page);
        await page.HumanFillAsync(sel, value);
        Assert.Equal(value, await page.InputValueAsync(sel));
        await AssertHumanKeys(page, sel[1..]);   // typed, not set
        Assert.Empty(await Pressed(page, "Backspace", sel[1..]));
    }));

    [Fact]
    public Task Keyboard_type_into_a_focused_password_makes_no_slips() => Live(page => WithTypoRate(0.6, async () =>
    {
        await page.HumanClickAsync("#pw");
        await Reset(page);
        await page.HumanTypeAsync("abcdefgh");
        Assert.Equal("abcdefgh", await page.InputValueAsync("#pw"));
        Assert.Empty(await Pressed(page, "Backspace", "pw"));
    }));

    [Fact]
    public Task Typos_respect_maxlength() => Live(page => WithTypoRate(1.0, async () =>
    {
        for (int i = 0; i < 3; i++)
        {
            await page.HumanFillAsync("#zip", "12345");
            Assert.Equal("12345", await page.InputValueAsync("#zip"));
        }
        await AssertHumanKeys(page, "zip");
    }));

    [Fact]
    public Task Typos_in_a_masked_field_never_delete_real_digits() => Live(page => WithTypoRate(1.0, async () =>
    {
        for (int i = 0; i < 3; i++)
        {
            await page.HumanFillAsync("#masked", "24680");
            Assert.Equal("24680", await page.InputValueAsync("#masked"));
        }
        await AssertHumanKeys(page, "masked");
    }));

    // ------------------------------------------------------------------------------------ select
    [Fact]
    public Task Select_by_label_fires_one_trusted_change() => Live(async page =>
    {
        await Reset(page);
        Assert.Equal(new[] { "ger25" }, await page.HumanSelectOptionAsync("#country", "Germany"));
        Assert.Equal("ger25", await page.InputValueAsync("#country"));
        var ch = await Changes(page, "country");
        Assert.Single(ch);
        Assert.True(Tr(ch[0]));
        Assert.NotEmpty(await Ev(page, "mousedown", "country"));   // the drop-down was opened with the mouse
    });

    [Theory]
    [InlineData("value", "swi50", "swi50")]
    [InlineData("index", "3", "and3")]
    [InlineData("label", "Japan", "jap35")]
    [InlineData("value", "United Kingdom", "uni53")]
    public Task Select_value_index_label(string by, string arg, string want) => Live(async page =>
    {
        await Reset(page);
        var got = by switch
        {
            "index" => await page.HumanSelectOptionAsync("#country", new SelectOptionValue { Index = int.Parse(arg) }),
            "label" => await page.HumanSelectOptionAsync("#country", new SelectOptionValue { Label = arg }),
            _ => await page.HumanSelectOptionAsync("#country", arg),
        };
        Assert.Equal(new[] { want }, got);
        Assert.Equal(want, await page.InputValueAsync("#country"));
        await AssertOneTrustedChange(page, "country");
    });

    [Fact]
    public Task Select_through_a_locator() => Live(async page =>
    {
        await Reset(page);
        Assert.Equal(new[] { "fra23" }, await page.Locator("#country").HumanSelectOptionAsync("France"));
        await AssertOneTrustedChange(page, "country");
    });

    [Fact]
    public Task Select_far_down_a_long_list_is_quick() => Live(async page =>
    {
        await Reset(page);
        var sw = Stopwatch.StartNew();
        await page.HumanSelectOptionAsync("#country", "Uruguay");
        Assert.Equal("uru55", await page.InputValueAsync("#country"));
        await AssertOneTrustedChange(page, "country");
        Assert.True(sw.Elapsed.TotalSeconds < 10);
    });

    [Fact]
    public Task Select_the_current_option_fires_nothing() => Live(async page =>
    {
        await Reset(page);
        Assert.Equal(new[] { "afg0" }, await page.HumanSelectOptionAsync("#country", "afg0"));
        Assert.Empty(await Changes(page, "country"));
    });

    [Fact]
    public Task Select_duplicate_labels() => Live(async page =>
    {
        await Reset(page);
        await page.HumanSelectOptionAsync("#dup", "p2");
        Assert.Equal("p2", await page.InputValueAsync("#dup"));
        await AssertOneTrustedChange(page, "dup");
    });

    [Fact]
    public Task Select_skips_disabled_rows() => Live(async page =>
    {
        await Reset(page);
        await page.HumanSelectOptionAsync("#dis", "e");
        Assert.Equal("e", await page.InputValueAsync("#dis"));
        await AssertOneTrustedChange(page, "dis");
    });

    [Fact]
    public Task List_box_scrolls_with_the_wheel() => Live(async page =>
    {
        await Reset(page);
        await page.HumanSelectOptionAsync("#lb", "opt17");
        Assert.Equal("opt17", await page.InputValueAsync("#lb"));
        Assert.NotEmpty(await Ev(page, "wheel"));   // the box is scrolled by the wheel, not by script
        var ch = await Changes(page, "lb");
        Assert.NotEmpty(ch);
        Assert.All(ch, c => Assert.True(Tr(c)));
    });

    [Fact]
    public Task Multi_select_with_ctrl_click() => Live(async page =>
    {
        await Reset(page);
        var got = await page.HumanSelectOptionAsync("#ms", new[] { "b", "d", "g" });
        Assert.Equal(new[] { "b", "d", "g" }, got.OrderBy(v => v, StringComparer.Ordinal).ToArray());
        Assert.Equal(new[] { "b", "d", "g" },
            await page.EvalOnSelectorAsync<string[]>("#ms", "e => [...e.selectedOptions].map(o => o.value)"));
        Assert.True((await Pressed(page, "Control")).Count > 0 || (await Pressed(page, "Meta")).Count > 0);
        Assert.All(await Changes(page, "ms"), c => Assert.True(Tr(c)));
    });

    // ------------------------------------------------------------------------------------- range
    [Fact]
    public Task Range_reaches_the_exact_value() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#rng", "145");
        Assert.Equal("145", await page.InputValueAsync("#rng"));
        var inputs = await Ev(page, "input", "rng");
        Assert.NotEmpty(inputs);
        Assert.All(inputs, e => Assert.True(Tr(e)));
        Assert.NotEmpty(await Ev(page, "pointerdown", "rng"));   // moved with the pointer, not set by script
    });

    [Fact]
    public Task Range_values_playwright_rejects_are_rejected() => Live(async page =>
    {
        // Off the step, out of range, not a number: Playwright raises "Malformed value"; the humanized fill
        // must too, never a silently snapped or clamped value.
        foreach (var bad in new[] { "13", "250", "nan" })
            await Assert.ThrowsAnyAsync<PlaywrightException>(() => page.HumanFillAsync("#rng2", bad, new PageFillOptions { Timeout = 3000 }));
        await page.HumanFillAsync("#rng2", "15");
        Assert.Equal("15", await page.InputValueAsync("#rng2"));
    });

    [Fact]
    public Task Range_right_to_left() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#rngrtl", "30");
        Assert.Equal("30", await page.InputValueAsync("#rngrtl"));
        Assert.NotEmpty(await Ev(page, "pointerdown", "rngrtl"));   // moved with the pointer, not set by script
    });

    // ---------------------------------------------------------------------------- date and time
    [Theory]
    [InlineData("#d", "2026-10-11")]
    [InlineData("#dpre", "2026-10-11")]
    [InlineData("#t", "17:45")]
    [InlineData("#t", "09:05")]
    [InlineData("#ts", "17:45:30")]
    [InlineData("#m", "2026-10")]
    [InlineData("#w", "2026-W41")]
    [InlineData("#dtl", "2026-10-11T09:30")]
    public Task Date_like_fields_are_typed_by_segment(string sel, string value) => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync(sel, value);
        Assert.Equal(value, await page.InputValueAsync(sel));
        var ident = sel[1..];
        Assert.NotEmpty(await Ev(page, "keydown", ident));     // typed with the keyboard, not set by script
        Assert.NotEmpty(await Ev(page, "mousedown", ident));   // clicked into the field first
        Assert.All((await Ev(page)).Where(e => S(e, "id") == ident && S(e, "t") is "input" or "change"), e => Assert.True(Tr(e)));
    });

    [Fact]
    public Task Color_goes_through_playwright() => Live(async page =>
    {
        await page.HumanFillAsync("#col", "#ff0000");
        Assert.Equal("#ff0000", await page.InputValueAsync("#col"));
    });

    [Fact]
    public Task Fill_a_checkbox_raises_like_playwright() => Live(async page =>
    {
        await Assert.ThrowsAnyAsync<PlaywrightException>(() => page.HumanFillAsync("#cb", "x", new PageFillOptions { Timeout = 2000 }));
    });

    // ---------------------------------------------------------------- the page never sees the reads
    [Fact]
    public Task Form_routes_make_no_main_world_reads() => Live(async page =>
    {
        // Hook the prototypes humanize would use if it read the DOM from the page's own world.
        await page.EvaluateAsync(@"() => {
          window.__hits = [];
          for (const [obj, name] of [[Document.prototype, 'querySelector'], [Document.prototype, 'elementFromPoint'],
                                     [Element.prototype, 'getBoundingClientRect'], [Element.prototype, 'closest'],
                                     [Node.prototype, 'contains'], [window, 'getComputedStyle']]) {
            const orig = obj[name];
            obj[name] = function (...a) { window.__hits.push(name); return orig.apply(this, a); };
          }
          for (const [proto, p] of [[Document.prototype, 'activeElement'], [HTMLSelectElement.prototype, 'selectedIndex'],
                                    [HTMLSelectElement.prototype, 'options']]) {
            const d = Object.getOwnPropertyDescriptor(proto, p);
            Object.defineProperty(proto, p, { get() { window.__hits.push(p); return d.get.call(this); }, set: d.set, configurable: true });
          }
        }");
        await page.HumanFillAsync("#name", "abc");
        await page.HumanFillAsync("#rng", "100");
        await page.HumanFillAsync("#d", "2026-10-11");
        await page.HumanSelectOptionAsync("#country", "Spain");
        await page.HumanCheckAsync("#cb");
        await page.HumanClickAsync("#btn", new HumanClickOptions { Position = new Position { X = 5, Y = 5 } });
        Assert.Empty(await page.EvaluateAsync<string[]>("window.__hits"));
        Assert.Equal("abc", await page.InputValueAsync("#name"));
        Assert.Equal("spa48", await page.InputValueAsync("#country"));
    });

    // ------------------------------------------------- cases the first verification found missing
    [Fact]
    public async Task First_click_on_fresh_pages_is_humanized()
    {
        // The covered-point probe used to time out on ~1 in 6 FIRST clicks of a fresh page (slow first
        // frames, not an overlay) and send a plain native click instead. Six fresh pages, every one glides.
        for (int i = 0; i < 6; i++)
            await Live(async page =>
            {
                await page.HumanClickAsync("#btn");
                await AssertHumanPress(page, "btn");
            });
    }

    [Fact]
    public Task Frame_locator_fill_replaces_the_value() => Live(async page =>
    {
        var loc = page.FrameLocator("#fr").Locator("#fi");
        Assert.Contains("internal:control=enter-frame", loc.ToString());   // what Humanize.IsFrameLocator reads
        Assert.True(Humanize.IsFrameLocator(loc));
        await loc.HumanFillAsync("new");
        Assert.Equal("new", await loc.InputValueAsync());
    });

    [Fact]
    public Task A_covered_field_still_ends_with_the_value() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#cov", "new", new PageFillOptions { Timeout = 5000 });
        Assert.Equal("new", await page.InputValueAsync("#cov"));
        var stray = (await Ev(page, "keydown")).Where(e => S(e, "id") is "" or "BODY" or "covlay").ToList();
        Assert.True(stray.Count == 0, "keys went to the page instead of the field");
    });

    [Fact]
    public Task A_reformatting_mask_is_typed_once() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#phone", "5551234567");
        Assert.Equal("(555) 123-4567", await page.InputValueAsync("#phone"));
        var digits = (await Ev(page, "keydown", "phone")).Count(e => S(e, "key") is { Length: 1 } k && char.IsAsciiDigit(k[0]));
        Assert.True(digits == 10, $"typed {digits} digits for a 10-digit number");
    });

    [Fact]
    public Task Force_on_hidden_targets_does_not_wait() => Live(async page =>
    {
        var sw = Stopwatch.StartNew();
        await page.HumanClickAsync("#hiddenbtn", new HumanClickOptions { Force = true, Timeout = 3000 });
        await page.HumanFillAsync("#hiddendate", "2026-10-11", new PageFillOptions { Force = true, Timeout = 3000 });
        Assert.True(sw.Elapsed.TotalSeconds < 2.5, $"{sw.Elapsed.TotalSeconds:F2}s");
        Assert.Equal("2026-10-11", await page.InputValueAsync("#hiddendate"));
    });

    [Fact]
    public Task Type_into_a_hidden_helper_textarea() => Live(async page =>
    {
        var sw = Stopwatch.StartNew();
        await page.Locator("#helper").HumanTypeAsync("abc");
        Assert.Equal("abc", await page.InputValueAsync("#helper"));
        Assert.True(sw.Elapsed.TotalSeconds < 2.5, $"{sw.Elapsed.TotalSeconds:F2}s");
    });

    [Fact]
    public Task Press_on_a_button_activates_it_once() => Live(async page =>
    {
        await page.Locator("#counter").HumanPressAsync("Enter");
        Assert.Equal(1, await page.EvaluateAsync<int>("window.__clicks || 0"));
        await page.Locator("#counter").HumanPressAsync("Enter");
        Assert.Equal(2, await page.EvaluateAsync<int>("window.__clicks || 0"));
    });

    [Fact]
    public Task The_callers_default_timeout_is_honoured() => Live(async page =>
    {
        page.SetDefaultTimeout(1500);
        var sw = Stopwatch.StartNew();
        await Assert.ThrowsAnyAsync<System.TimeoutException>(() => page.HumanClickAsync("#nope"));
        await Assert.ThrowsAnyAsync<System.TimeoutException>(() => page.HumanFillAsync("#nope", "x"));
        await Assert.ThrowsAnyAsync<System.TimeoutException>(() => page.HumanSelectOptionAsync("#nope", "x"));
        await Assert.ThrowsAnyAsync<System.TimeoutException>(() => page.HumanCheckAsync("#nope"));
        Assert.True(sw.Elapsed.TotalSeconds < 8.5, $"each wait must be the page's 1.5 s, not 30 s ({sw.Elapsed.TotalSeconds:F2}s)");
    });

    [Fact]
    public Task Check_on_a_checkbox_that_cancels_its_click_clicks_once_and_raises() => Live(async page =>
    {
        await Reset(page);
        await Assert.ThrowsAnyAsync<PlaywrightException>(() => page.Locator("#stubborn").HumanCheckAsync(new LocatorCheckOptions { Timeout = 3000 }));
        Assert.Single(await Ev(page, "click", "stubborn"));
    });

    [Fact]
    public Task Delay_zero_still_holds_the_button() => Live(async page =>
    {
        await Reset(page);
        await page.HumanClickAsync("#btn", new HumanClickOptions { Delay = 0 });
        var d = (await Ev(page, "mousedown", "btn"))[0];
        var u = (await Ev(page, "mouseup", "btn"))[0];
        Assert.True(D(u, "ts") - D(d, "ts") >= 40, "Playwright's default delay 0 is not a 1 ms press");
    });

    [Fact]
    public Task Select_all_holds_the_modifier_before_the_a() => Live(async page =>
    {
        await Reset(page);
        await page.HumanFillAsync("#prefilled", "x");
        var ks = (await Ev(page)).Where(e => S(e, "t") is "keydown" or "keyup" && S(e, "id") == "prefilled").ToList();
        var modDown = ks.First(e => S(e, "t") == "keydown" && S(e, "key") is "Control" or "Meta");
        var aDown = ks.First(e => S(e, "t") == "keydown" && S(e, "key") == "a");
        var aUp = ks.First(e => S(e, "t") == "keyup" && S(e, "key") == "a");
        var modUp = ks.First(e => S(e, "t") == "keyup" && S(e, "key") is "Control" or "Meta");
        Assert.True(D(aDown, "ts") - D(modDown, "ts") >= 30, "the modifier leads the A like a finger");
        Assert.True(D(modUp, "ts") - D(aUp, "ts") >= 15, "and comes up just after it");
    });

    [Fact]
    public Task A_select_under_a_floating_overlay_opens_and_picks_once() => Live(async page =>
    {
        await Reset(page);
        Assert.Equal(new[] { "e" }, await page.HumanSelectOptionAsync("#selfloat", "e"));
        Assert.Equal("e", await page.InputValueAsync("#selfloat"));
        await AssertOneTrustedChange(page, "selfloat");
    });

    [Fact]
    public Task Typo_gate_reads_through_shadow_roots_and_fails_closed_in_frames() => Live(async page =>
    {
        await page.HumanClickAsync("#spw #inner");
        var f = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.ActiveField);
        Assert.Equal("password", f!.Value.GetProperty("type").GetString());
        await page.FrameLocator("#fr").Locator("#fpw").ClickAsync();
        f = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.ActiveField);
        Assert.True(f!.Value.GetProperty("frame").GetBoolean());
    });

    [Fact]
    public Task No_slips_in_a_password_inside_a_shadow_root() => Live(page => WithTypoRate(1.0, async () =>
    {
        await Reset(page);
        await page.HumanFillAsync("#spw #inner", "abcdef");
        Assert.Equal("abcdef", await page.InputValueAsync("#spw #inner"));
        Assert.Empty(await Pressed(page, "Backspace", "spw"));
    }));

    [Fact]
    public Task Two_concurrent_fills_on_one_page_both_land() => Live(async page =>
    {
        // Task.WhenAll of two humanized fills on ONE page: they take turns (a per-page gate) instead of
        // interleaving clicks and keys into each other's field.
        await Task.WhenAll(page.HumanFillAsync("#name", "Ada Lovelace"), page.HumanFillAsync("#city", "Paris"),
            page.Locator("#bio").HumanFillAsync("two words"));
        Assert.Equal("Ada Lovelace", await page.InputValueAsync("#name"));
        Assert.Equal("Paris", await page.InputValueAsync("#city"));
        Assert.Equal("two words", await page.InputValueAsync("#bio"));
    });

    // ------------------------------------------------------- cases verifier B found (SC 197, verify-final-B)
    // Counts the reads of Node.textContent made in the PAGE's world (a Playwright element handle's preview
    // makes one); the fixture's own recorder uses a getter saved before this hook.
    private const string CountTextReads = @"() => { const d = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  window.__reads = 0;
  Object.defineProperty(Node.prototype, 'textContent', { configurable: true, enumerable: d.enumerable,
    get() { window.__reads++; return d.get.call(this); }, set(v) { d.set.call(this, v); } }); }";
    // Where the pointer went, move by move -- every COALESCED move: the renderer merges a frame's moves into
    // one event, so a glide never steps far between two of them, the old trial's jump onto the target did --
    // and every pointerdown a WINDOW capture listener sees (it runs before anything Playwright installs during
    // an action, so a press Playwright blocks is still seen).
    private const string TrackPointer = @"() => { window.__mv = []; window.__downs = [];
  addEventListener('pointermove', e => { const l = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    for (const c of (l.length ? l : [e])) __mv.push([c.clientX, c.clientY]); }, true);
  addEventListener('pointerdown', e => __downs.push([e.target.id, e.button, e.isTrusted]), true); }";

    private static async Task<string> DownsAsync(IPage page)
        => (await page.EvaluateAsync<JsonElement>("JSON.stringify(__downs)")).GetString()!;

    [Fact]
    public Task Long_list_is_typed_through_its_spaced_label() => Live(async page =>
    {
        // "Item 371" of 400 "Item NNN": the space is typed too. Without it the prefix stopped at "item" and
        // the other 370 rows were single ArrowDowns -- 68 s.
        await Reset(page);
        var sw = Stopwatch.StartNew();
        Assert.Equal(new[] { "v371" }, await page.HumanSelectOptionAsync("#long", "Item 371"));
        Assert.True(sw.Elapsed.TotalSeconds < 15, $"{sw.Elapsed.TotalSeconds:F1}s");
        var ch = await Changes(page, "long");
        Assert.True(ch.Count == 1 && Tr(ch[0]), $"{ch.Count} changes{Why}");
    });

    [Fact]
    public Task Untypeable_long_list_holds_the_arrow_key() => Live(async page =>
    {
        await Reset(page);
        var sw = Stopwatch.StartNew();
        Assert.Equal(new[] { "s100" }, await page.HumanSelectOptionAsync("#same", "s100"));
        Assert.True(sw.Elapsed.TotalSeconds < 20, $"100 rows are crossed with a held key, not 100 separate presses ({sw.Elapsed.TotalSeconds:F1}s)");
        var ch = await Changes(page, "same");
        Assert.True(ch.Count == 1 && Tr(ch[0]), $"{ch.Count} changes{Why}");
    });

    [Theory]
    [InlineData("m10")]
    [InlineData("m14")]
    [InlineData("m03")]
    public Task List_box_row_that_needs_a_mid_list_scroll(string value) => Live(async page =>
    {
        // The old loop read the box mid-animation, wheeled the wrong way and oscillated, then fell back to an
        // untrusted native change.
        await page.EvaluateAsync("document.getElementById('lbmid').scrollTop = 0");
        await Reset(page);
        Assert.Equal(new[] { value }, await page.HumanSelectOptionAsync("#lbmid", value));
        Assert.Equal(value, await page.InputValueAsync("#lbmid"));
        Assert.True((await Ev(page)).All(Tr), "every event trusted: no native fallback" + Why);
        var wheels = (await Ev(page, "wheel")).Count;
        Assert.True(wheels <= 6, $"{wheels} wheel events");
        Assert.NotEmpty(await Ev(page, "mousedown", "OPTION"));   // the row itself was clicked
    });

    [Fact]
    public Task A_click_does_not_jump_the_pointer_or_press_twice() => Live(async page =>
    {
        // The covered-point check used to be a Playwright TRIAL click, which moved the real pointer onto the
        // target and back and fired a pointerdown that only its own listener blocked.
        await page.EvaluateAsync(TrackPointer);
        // Put the pointer near the top of the page first, as the Python test's (humanized) mouse.move(60, 60)
        // does. .NET has no humanized raw move, and a cursor .NET seeds at a random point can start below the
        // fixture's iframe: a glide that crosses #fr delivers its moves to the iframe's document, not this one,
        // and the window listener then sees a "jump" the width of the crossing (measured: 96-195 px, 4 in 8).
        await page.HumanHoverAsync("#name");
        foreach (var (sel, button) in new[] { ("#btn", MouseButton.Left), ("#btn", MouseButton.Right), ("#counter", MouseButton.Left) })
        {
            await page.EvaluateAsync("__mv.length = 0; __downs.length = 0");
            await page.HumanClickAsync(sel, new HumanClickOptions { Button = button });
            var mv = (await page.EvaluateAsync<JsonElement>("__mv")).EnumerateArray()
                .Select(m => (X: m[0].GetDouble(), Y: m[1].GetDouble())).ToList();
            double jump = 0;
            int at = 0;
            for (int i = 1; i < mv.Count; i++)
            {
                double d = Math.Sqrt(Math.Pow(mv[i].X - mv[i - 1].X, 2) + Math.Pow(mv[i].Y - mv[i - 1].Y, 2));
                if (d > jump) (jump, at) = (d, i);
            }
            Assert.True(mv.Count > 0 && jump < 80,
                $"a jump of {jump:F0} px ({sel} {button}) at move {at} of {mv.Count}: "
                + string.Join(" ", mv.Skip(Math.Max(0, at - 3)).Take(6).Select(m => $"({m.X:F0},{m.Y:F0})"))
                + Why);
            Assert.Equal($"[[\"{sel[1..]}\",{(button == MouseButton.Left ? 0 : 2)},true]]", await DownsAsync(page));
        }
    });

    [Fact]
    public Task Dblclick_presses_twice_not_three_times() => Live(async page =>
    {
        await page.EvaluateAsync(TrackPointer);
        await page.EvaluateAsync("__downs.length = 0");
        await page.HumanDblClickAsync("#btn");
        Assert.Equal("[[\"btn\",0,true],[\"btn\",0,true]]", await DownsAsync(page));
    });

    [Fact]
    public Task Css_targets_cause_no_main_world_reads() => Live(async page =>
    {
        // What the CSS fast path reads off a locator (its own "Locator@selector" description):
        Assert.Equal("#btn", Humanize.CssOf(page.Locator("#btn")));
        Assert.Equal("#btn", Humanize.CssOf(page.Locator("#btn").First));
        Assert.Null(Humanize.CssOf(page.GetByRole(AriaRole.Button, new PageGetByRoleOptions { Name = "Target" })));
        Assert.Null(Humanize.CssOf(page.Locator("#btn").Nth(1)));
        await page.EvaluateAsync(CountTextReads);
        await page.HumanClickAsync("#btn");
        await page.Locator("#btn").HumanClickAsync();
        await page.HumanDblClickAsync("#btn");
        await page.HumanHoverAsync("#btn");
        await page.HumanFillAsync("#bio", "two words");
        await page.Locator("#bio").HumanTypeAsync(" more");
        await page.HumanFillAsync("#ce", "edited");
        await page.HumanClickAsync("#far");
        Assert.Equal(0, await page.EvaluateAsync<int>("__reads"));
        Assert.Equal("two words more", await page.InputValueAsync("#bio"));
    });

    [Fact]
    public Task Role_targets_cause_at_most_one_main_world_read() => Live(async page =>
    {
        // GetByRole cannot be resolved without Playwright's BoundingBoxAsync, whose handle preview reads
        // textContent once; scrolling the window to an off-screen one makes no further handle.
        await page.EvaluateAsync(CountTextReads);
        await page.GetByRole(AriaRole.Button, new PageGetByRoleOptions { Name = "Target" }).HumanClickAsync();
        Assert.True(await page.EvaluateAsync<int>("__reads") <= 1);
        await page.EvaluateAsync("scrollTo(0, 0); __reads = 0");
        await Reset(page);
        await page.GetByRole(AriaRole.Button, new PageGetByRoleOptions { Name = "Far away" }).HumanClickAsync();
        Assert.True(await page.EvaluateAsync<int>("__reads") <= 1);
        Assert.Equal(new[] { true }, (await Ev(page, "click", "far")).Select(Tr).ToArray());
    });

    [Theory]
    [InlineData("css")]
    [InlineData("role")]
    public Task Click_a_button_clipped_by_a_scrolling_container(string how) => Live(async page =>
    {
        // In the viewport, but cut off by its overflow:auto parent: the hit test sees that, and the container
        // is scrolled before the humanized click (it is not handed to a native click).
        await page.EvaluateAsync("document.getElementById('scroller').scrollIntoView({block: 'center'});" +
                                 "document.getElementById('scroller').scrollTop = 0");
        await Reset(page);
        if (how == "css") await page.HumanClickAsync("#innerbtn");
        else await page.GetByRole(AriaRole.Button, new PageGetByRoleOptions { Name = "Inner one" }).HumanClickAsync();
        Assert.Equal(new[] { true }, (await Ev(page, "click", "innerbtn")).Select(Tr).ToArray());
        Assert.True(await page.EvaluateAsync<double>("document.getElementById('scroller').scrollTop") > 0);
        Assert.NotEmpty(await Ev(page, "mousemove"));   // the pointer travelled to it
    });

    // --------------------------------------------------------------- SC 210: pointer-events:none, trial
    private static async Task<(List<JsonElement> Main, List<JsonElement> Inner)> Wev(IPage page)
    {
        var both = await page.EvaluateAsync<JsonElement>("[window.__wev, window.__fev || []]");
        return (both[0].EnumerateArray().Select(e => e.Clone()).ToList(), both[1].EnumerateArray().Select(e => e.Clone()).ToList());
    }

    private static Task ResetW(IPage page) => page.EvaluateAsync("window.__wev.length = 0; (window.__fev || []).length = 0");

    private static async Task<int> Moves(IPage page) => (await Ev(page, "mousemove")).Sum(e => (int)D(e, "n"));

    [Fact]
    public Task Controls_under_a_pointer_events_none_layer_are_humanized() => Live(async page =>
    {
        // A real press goes through such a layer, so the hit test must skip it too. With ignorePointerEventsNone
        // the layer answered and fill / select / click fell back to stock Playwright (SC 209: untrusted change, 6/6).
        await Reset(page);
        await page.HumanFillAsync("#pen", "glass");
        Assert.Equal("glass", await page.InputValueAsync("#pen"));
        await AssertHumanKeys(page, "pen");
        await Reset(page);
        await page.HumanSelectOptionAsync("#pensel", "c");
        Assert.Equal("c", await page.InputValueAsync("#pensel"));
        Assert.Equal(new[] { true }, (await Changes(page, "pensel")).Select(Tr).ToArray());
        await Reset(page);
        await page.HumanClickAsync("#penbtn");
        Assert.True(await Moves(page) >= 5, "the click glided (humanized), not a native jump" + Why);
        Assert.True(Tr((await Ev(page, "mousedown", "penbtn"))[0]));
    });

    [Fact]
    public Task Trial_moves_and_presses_nothing_a_window_listener_could_see() => Live(async page =>
    {
        // Playwright's own trial moved the pointer onto the target and dispatched pointerdown/pointerup/click that
        // listeners registered before its blocker saw (SC 209).
        await page.Locator("#trialbtn").ScrollIntoViewIfNeededAsync();
        await page.Locator("#frb").ScrollIntoViewIfNeededAsync();
        await ResetW(page);
        await page.HumanClickAsync("#trialbtn", new HumanClickOptions { Trial = true });
        await page.Locator("#trialbtn").HumanClickAsync(new LocatorClickOptions { Trial = true });
        await page.HumanDblClickAsync("#trialbtn", new HumanClickOptions { Trial = true });
        await page.HumanHoverAsync("#trialbtn", new HumanClickOptions { Trial = true });
        await page.HumanClickAsync("#trialbtn", new HumanClickOptions { Trial = true, Position = new Position { X = 5, Y = 5 } });
        await page.HumanCheckAsync("#cb", new PageCheckOptions { Trial = true });
        await page.Locator("#cb").HumanSetCheckedAsync(true, new LocatorSetCheckedOptions { Trial = true });
        await page.HumanClickAsync("#frb", new HumanClickOptions { Trial = true });   // the click point lies inside a child frame
        var (main, inner) = await Wev(page);
        Assert.True(main.Count == 0 && inner.Count == 0, "a trial dispatched pointer events: "
            + string.Join(",", main.Concat(inner).Select(e => S(e, "t") + "@" + S(e, "id"))));
        Assert.False(await page.IsCheckedAsync("#cb"));
    });

    [Fact]
    public Task Trial_fails_like_Playwright_when_a_check_never_passes() => Live(async page =>
    {
        await page.Locator("#disbtn").ScrollIntoViewIfNeededAsync();
        await ResetW(page);
        foreach (var sel in new[] { "#disbtn", "#hiddenbtn", "#cov" })   // disabled; hidden; covered by #covlay
        {
            var sw = Stopwatch.StartNew();
            await Assert.ThrowsAsync<System.TimeoutException>(
                () => page.HumanClickAsync(sel, new HumanClickOptions { Trial = true, Timeout = 600 }));
            Assert.True(sw.ElapsedMilliseconds < 3000, sel);
        }
        page.SetDefaultTimeout(700);   // no Timeout: the page default, as in Playwright
        var sw2 = Stopwatch.StartNew();
        await Assert.ThrowsAsync<System.TimeoutException>(
            () => page.HumanClickAsync("#disbtn", new HumanClickOptions { Trial = true }));
        Assert.True(sw2.ElapsedMilliseconds < 3000, "the page default timeout");
        await page.HumanHoverAsync("#disbtn", new HumanClickOptions { Trial = true, Timeout = 600 });   // hover does not need enabled
        var (main, inner) = await Wev(page);
        Assert.Empty(main);
        Assert.Empty(inner);
    });

    [Fact]
    public Task A_click_on_a_frame_is_humanized_without_a_trial() => Live(async page =>
    {
        // The click point of an <iframe> lies in its child frame: the hit test answers it with the <iframe>
        // element, so the click glides and presses with no Playwright trial before it.
        await page.Locator("#frb").ScrollIntoViewIfNeededAsync();
        await Reset(page);
        await ResetW(page);
        await page.HumanClickAsync("#frb");
        var (main, inner) = await Wev(page);
        Assert.Equal(new[] { true }, inner.Where(e => S(e, "t") == "pointerdown").Select(Tr).ToArray());
        Assert.True(await Moves(page) >= 5, "the click glided (humanized)" + Why);
        Assert.DoesNotContain(main, e => S(e, "t") is "pointerdown" or "mousedown" or "click");
    });
}
