using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// One headless Clearcote browser for the live press-and-hold suite (a persistent context in a temp profile that is
/// removed afterwards). Nothing is launched unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
public sealed class HoldLiveBrowser : IAsyncLifetime
{
    public static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
    public IBrowserContext? Context { get; private set; }
    public string? Dir { get; private set; }
    private static readonly string FcCache = Path.Join(Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar), "cc-fc-cache");
    private bool _fcCacheExisted = true;

    public async Task InitializeAsync()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        _fcCacheExisted = Directory.Exists(FcCache);
        Dir = TestTemp.Create("cc-live-hold-");
        Context = await Clearcote.LaunchPersistentContextAsync(Path.Combine(Dir, "profile"), new LaunchOptions
        {
            ExecutablePath = LiveExe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true,
            Fingerprint = "hold-live-1",
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
            if (Dir is not null) TestTemp.Remove(Dir);
            if (!_fcCacheExisted) TestTemp.Remove(FcCache);
        }
    }
}

/// LIVE PressAndHoldAsync against the press-and-hold widget (sdk/testdata/press_and_hold.html; ?ms=N fixes the hold
/// it needs), mirroring sdk/python/tests/test_press_and_hold_live.py. Skipped (a no-op, as in HumanizeLiveTests)
/// unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary; a PRO build also needs a licence (CLEARCOTE_LICENSE_KEY or
/// the saved key). What is asserted is what the PAGE sees: window.__hold's state, attempts, resets, rejected
/// presses, and the pointer moves seen while held with no button reported.
public class PressAndHoldLiveTests : IClassFixture<HoldLiveBrowser>
{
    private readonly HoldLiveBrowser _b;
    public PressAndHoldLiveTests(HoldLiveBrowser b) => _b = b;

    private static string WidgetUrl(int ms) => new Uri(SharedTestData.PathOf("press_and_hold.html")).AbsoluteUri + "?ms=" + ms;

    private static async Task<JsonElement> StateAsync(IFrame f)
        => JsonDocument.Parse(await f.EvaluateAsync<string>("JSON.stringify(window.__hold)")).RootElement;

    private static string[] Whys(JsonElement s) => s.GetProperty("resets").EnumerateArray().Select(r => r.GetProperty("why").GetString()!).ToArray();
    private static string[] Rejected(JsonElement s) => s.GetProperty("rejected").EnumerateArray().Select(r => r.GetString()!).ToArray();

    private static void AssertDone(JsonElement s)
    {
        var raw = s.GetRawText();
        Assert.True(s.GetProperty("state").GetString() == "done", raw);
        Assert.True(s.GetProperty("attempts").GetInt32() == 1, raw);
        Assert.True(Whys(s).Length == 0 && Rejected(s).Length == 0, raw);
        Assert.True(s.GetProperty("movesNoButton").GetInt32() == 0, raw);
    }

    private async Task Live(bool humanized, Func<IPage, Task> body)
    {
        if (_b.Context is null) return;
        var page = await _b.Context.NewPageAsync();
        if (humanized) Humanize.Attach(page, "hold-live-1");
        try
        {
            await body(page);
        }
        finally
        {
            await page.CloseAsync();
        }
    }

    [Fact]
    public Task Widget_rejects_a_plain_click_a_synthetic_press_and_a_short_hold() => Live(true, async page =>
    {
        // The widget is a real gate: a stock click, a synthetic press and a too-short hold all fail.
        await page.GotoAsync(WidgetUrl(1500));
        await page.Locator("#hold").ClickAsync();
        await page.EvaluateAsync("document.getElementById('hold').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))");
        Assert.True(await page.PressAndHoldAsync("#hold", new PressAndHoldOptions { Duration = 600 }));
        var s = await StateAsync(page.MainFrame);
        Assert.True(s.GetProperty("state").GetString() == "idle", s.GetRawText());
        Assert.True(s.GetProperty("attempts").GetInt32() == 2, s.GetRawText());
        Assert.Equal(new[] { "released", "released" }, Whys(s));
        Assert.Equal(new[] { "untrusted" }, Rejected(s));
    });

    [Fact]
    public Task Humanized_hold_until_done_in_the_main_frame() => Live(true, async page =>
    {
        await page.GotoAsync(WidgetUrl(2500));
        Assert.True(Humanize.InMainFrame(page.Locator("#hold")));
        var t0 = DateTime.UtcNow;
        var ok = await page.Locator("#hold").PressAndHoldAsync(new PressAndHoldOptions
        {
            Until = async () => await page.Locator("#hold[data-state=done]").CountAsync() > 0,
        });
        Assert.True(ok);
        var s = await StateAsync(page.MainFrame);
        AssertDone(s);
        Assert.True(s.GetProperty("moves").GetInt32() >= 1, s.GetRawText());   // a 2.5 s hold drifts
        Assert.Equal("Verified", await page.Locator("#status").InnerTextAsync());
        Assert.True(DateTime.UtcNow - t0 < TimeSpan.FromSeconds(10));
    });

    [Fact]
    public Task Humanized_hold_in_an_iframe_through_a_frame_locator_and_a_frame() => Live(true, async page =>
    {
        var file = Path.Combine(_b.Dir!, "widget-frame.html");
        await File.WriteAllTextAsync(file, "<!doctype html><body style='margin:0'><iframe id='f' src='" + WidgetUrl(2000) +
            "' style='position:absolute;left:120px;top:60px;width:640px;height:420px;border:0'></iframe>");
        await page.GotoAsync(new Uri(file).AbsoluteUri);
        var fl = page.FrameLocator("#f");
        var btn = fl.Locator("#hold");
        await btn.WaitForAsync();
        Assert.False(Humanize.InMainFrame(btn));
        Assert.True(await btn.PressAndHoldAsync(new PressAndHoldOptions
        {
            Until = async () => await fl.Locator("#hold[data-state=done]").CountAsync() > 0,
        }));
        var child = page.Frames.First(f => f.ParentFrame is not null);
        AssertDone(await StateAsync(child));

        // the same, through a locator made on the child IFrame (not a FrameLocator): told apart from the main frame
        await child.GotoAsync(WidgetUrl(1200));
        var onFrame = child.Locator("#hold");
        Assert.False(Humanize.InMainFrame(onFrame));
        Assert.True(await onFrame.PressAndHoldAsync(new PressAndHoldOptions { Duration = 1500 }));
        AssertDone(await StateAsync(child));
    });

    [Fact]
    public Task Plain_page_without_a_persona() => Live(false, async page =>
    {
        await page.GotoAsync(WidgetUrl(1700));
        Assert.True(await page.PressAndHoldAsync("#hold", new PressAndHoldOptions { Duration = 2000 }));
        var s = await StateAsync(page.MainFrame);
        AssertDone(s);
        Assert.True(s.GetProperty("moves").GetInt32() >= 1, s.GetRawText());
    });

    [Fact]
    public Task Until_that_never_turns_true_returns_false_after_the_duration() => Live(true, async page =>
    {
        await page.GotoAsync(WidgetUrl(5000));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        Assert.False(await page.PressAndHoldAsync("#hold", new PressAndHoldOptions
        {
            Duration = 700, Tremor = false, Until = () => Task.FromResult(false),
        }));
        var s = await StateAsync(page.MainFrame);
        Assert.Equal(new[] { "released" }, Whys(s));
        Assert.True(s.GetProperty("moves").GetInt32() == 0, s.GetRawText());   // Tremor = false: no moves while held
    });

    [Fact]
    public Task Cancelled_hold_still_releases_the_button() => Live(true, async page =>
    {
        await page.GotoAsync(WidgetUrl(5000));
        await page.Locator("#hold").HumanHoverAsync();   // the approach is done before the timer starts
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(1.5));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => page.PressAndHoldAsync("#hold", new PressAndHoldOptions { Duration = 5000 }, cts.Token));
        var s = await StateAsync(page.MainFrame);
        Assert.True(s.GetProperty("state").GetString() == "idle", s.GetRawText());
        Assert.Equal(new[] { "released" }, Whys(s));
        // and the page is not left in a drag: a later press works
        await page.GotoAsync(WidgetUrl(800));
        Assert.True(await page.PressAndHoldAsync("#hold", new PressAndHoldOptions { Duration = 1000 }));
        AssertDone(await StateAsync(page.MainFrame));
    });
}
