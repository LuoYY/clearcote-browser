using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Lane F (SC 232): the label draw's randomness rule, and the new .NET methods -- HumanClickAsync(page, x, y),
/// HumanClearAsync, HumanPressSequentiallyAsync -- live against a real Clearcote build (skipped without
/// CLEARCOTE_LIVE_ENGINE). None of these existed in 0.51.0 (this file does not compile there).
public class HumanizeCoverageTests : IClassFixture<FormsLiveBrowser>
{
    private const double MinHoldMs = 25;
    private readonly FormsLiveBrowser _b;
    public HumanizeCoverageTests(FormsLiveBrowser b) => _b = b;

    [Fact]
    public void Same_seed_same_label_trait_but_unseeded_per_click_choices()
    {
        // The seed fixes the TRAIT (how often this identity clicks the label); each click's choice is an
        // unseeded draw, or every session of an identity would choose the same way in the same order.
        var a = Motion.MakePersona("same-identity");
        var b = Motion.MakePersona("same-identity");
        Assert.Equal(a.Seed, b.Seed);
        var da = Enumerable.Range(0, 20).Select(_ => Humanize.LabelDraw(a)).ToList();
        var db = Enumerable.Range(0, 20).Select(_ => Humanize.LabelDraw(b)).ToList();
        Assert.NotEqual(da, db);
        var share = Enumerable.Range(0, 4000).Count(_ => Humanize.LabelDraw(a)) / 4000.0;
        // a persona trait now (lane B's aim traits, 0.25-0.55), not one 0.4 for every identity
        Assert.Equal(Humanize.LabelShareOf(a), Humanize.LabelShareOf(b));
        Assert.InRange(Humanize.LabelShareOf(a), 0.25, 0.55);
        Assert.InRange(share, Humanize.LabelShareOf(a) - 0.05, Humanize.LabelShareOf(a) + 0.05);
        Assert.Equal(Enumerable.Range(0, 20).Select(_ => Humanize.LabelDraw(a, new Random(7))),
                     Enumerable.Range(0, 20).Select(_ => Humanize.LabelDraw(a, new Random(7))));
    }

    private async Task Live(Func<IPage, Task> body)
    {
        if (_b.Context is null) return;
        var page = await _b.Context.NewPageAsync();
        try
        {
            Humanize.Attach(page, "coverage-live");
            Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
            await page.SetContentAsync(HumanizeLabelLiveTests.Html);
            await body(page);
        }
        finally
        {
            await page.CloseAsync();
        }
    }

    private static double Ts(JsonElement e) => e.GetProperty("ts").GetDouble();

    [Fact]
    public Task Coordinate_click_is_a_glide_and_a_held_trusted_press() => Live(async page =>
    {
        var box = (await page.Locator("#btn").BoundingBoxAsync())!;
        await page.HumanClickAsync(box.X + box.Width / 2, box.Y + box.Height / 2);
        var downs = await HumanizeLabelLiveTests.Ev(page, "mousedown");
        var ups = await HumanizeLabelLiveTests.Ev(page, "mouseup");
        Assert.Single(downs);
        Assert.Equal("btn", downs[0].GetProperty("id").GetString());
        Assert.True(downs[0].GetProperty("tr").GetBoolean());
        Assert.True(Ts(ups[0]) - Ts(downs[0]) >= MinHoldMs, "a finger's hold");
        var clicks = await HumanizeLabelLiveTests.Ev(page, "click");
        Assert.Single(clicks);
    });

    [Fact]
    public Task Clear_empties_the_field_with_held_trusted_keys() => Live(async page =>
    {
        await page.Locator("#t").HumanClearAsync();
        Assert.Equal("", await page.Locator("#t").InputValueAsync());
        var downs = await HumanizeLabelLiveTests.Ev(page, "keydown");
        Assert.NotEmpty(downs);
        Assert.All(downs, d => Assert.True(d.GetProperty("tr").GetBoolean()));
        Assert.Contains(downs, d => d.GetProperty("key").GetString() == "Backspace");
        Assert.Single(await HumanizeLabelLiveTests.Ev(page, "mousedown"));   // clicked into first
    });

    [Fact]
    public Task Press_sequentially_types_with_human_holds() => Live(async page =>
    {
        await page.Locator("#t").HumanClearAsync();
        await page.EvaluateAsync("window.__ev.length = 0");
        await page.Locator("#t").HumanPressSequentiallyAsync("hello there");
        Assert.Equal("hello there", await page.Locator("#t").InputValueAsync());
        var downs = await HumanizeLabelLiveTests.Ev(page, "keydown");
        var ups = await HumanizeLabelLiveTests.Ev(page, "keyup");
        Assert.Equal(11, downs.Count(d => d.GetProperty("key").GetString()!.Length == 1));
        Assert.All(downs, d => Assert.True(d.GetProperty("tr").GetBoolean()));
        // keydown -> the keyup of the same key (rollover can put the next keydown before this keyup)
        var holds = new List<double>();
        var open = new Dictionary<string, double>();
        foreach (var e in (await page.EvaluateAsync<JsonElement>("window.__ev")).EnumerateArray())
        {
            var t = e.GetProperty("t").GetString();
            var k = e.GetProperty("key").GetString()!;
            if (t == "keydown") open.TryAdd(k, Ts(e));
            else if (t == "keyup" && open.Remove(k, out var t0)) holds.Add(Ts(e) - t0);
        }
        holds.Sort();
        Assert.True(holds.Count >= 8 && holds[holds.Count / 2] >= MinHoldMs, $"median hold {(holds.Count > 0 ? holds[holds.Count / 2] : 0):F1}");
    });
}
