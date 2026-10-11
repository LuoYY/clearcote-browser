using System.Reflection;
using System.Text;
using System.Text.Json;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Persona window geometry (engine r39+): the engine opens each persona's own browser window.
/// </summary>
/// <remarks>
/// Until r38 every seed opened the same window while its screen varied (780x580 headless whenever the
/// SDK's fit did not run; the SDK's maximize otherwise). From r39 the engine derives the window from the
/// persona and applies it to the real window; <see cref="WindowGeom"/> is the bit-exact port. The vector
/// file (data/window_vectors.txt) is the engine's own output, from the standalone C++ test; the Python and
/// Node suites assert the same file. On an r39 engine a persona launch gets NO <c>--screen-info</c> and NO
/// fit, keeps NoViewport as its default, and an explicit viewport only moves a window that hangs over the
/// work area; on older engines nothing changes. Mirrors sdk/python/tests/test_window_geometry.py and
/// sdk/node/test/windowgeom.test.ts.
/// </remarks>
public class WindowGeometryTests : IDisposable
{
    private readonly List<string> _dirs = new();

    public void Dispose()
    {
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    // ─────────────────────────────────────────────────────── the port
    private static readonly string VectorFile = Path.Combine(AppContext.BaseDirectory, "data", "window_vectors.txt");

    private static List<string[]> Vectors() =>
        File.ReadAllLines(VectorFile)
            .Where(l => l.Trim().Length > 0)
            .Select(l => l.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
            .ToList();

    [Fact]
    public void VectorFileIsTheFullEngineTable()
    {
        var rows = Vectors();
        Assert.Equal(1536, rows.Count);   // regenerate with persona_window_geometry_test vectors
        Assert.Equal(new[] { "0", "1", "2" }, rows.Select(r => r[5]).Distinct().OrderBy(p => p, StringComparer.Ordinal));
        Assert.All(rows, r => Assert.Equal(14, r.Length));
    }

    [Fact]
    public void PortMatchesTheEngineBitForBit()
    {
        // Every rectangle and every saved-placement key equals the engine's (C++ standalone build).
        var bad = new List<string>();
        var checkedRows = 0;
        foreach (var r in Vectors())
        {
            var (raw, plat) = (r[0], int.Parse(r[5]));
            var (sw, sh, aw, ah) = (int.Parse(r[1]), int.Parse(r[2]), int.Parse(r[3]), int.Parse(r[4]));
            var seed = WindowGeom.MixedSeed(raw);
            var g = WindowGeom.DeriveWindow(seed, sw, sh, aw, ah, plat);
            var got = (g.Valid, g.Kind ?? "-", g.State ?? "normal", g.X, g.Y, g.Width, g.Height);
            var want = (r[6] == "1", r[7], r[8], int.Parse(r[9]), int.Parse(r[10]), int.Parse(r[11]), int.Parse(r[12]));
            var key = WindowGeom.WindowKey(seed, sw, sh, aw, ah, plat).ToString("x16");
            if (got != want || key != r[13]) bad.Add($"{string.Join(' ', r)} -> {got} key {key}");
            checkedRows++;
        }
        Assert.Equal(1536, checkedRows);
        Assert.True(bad.Count == 0, $"{bad.Count} rows differ from the engine, e.g.:\n" + string.Join("\n", bad.Take(5)));
    }

    [Fact]
    public void SeedParseMatchesTheEngine()
    {
        // PersonaSeedFromString: an unsigned decimal is the seed, anything else FNV-1a 64.
        Assert.Equal(0xAF63DC4C8601EC8CUL, WindowGeom.Fnv1a64("a"));   // the published FNV-1a 64 test value
        Assert.Equal(0xCBF29CE484222325UL, WindowGeom.Fnv1a64(""));
        // MixedPersonaSeed(0) is SplitMix64's first output for state 0
        Assert.Equal(0xE220A8397B1DCDAFUL, WindowGeom.MixedSeed("0"));
        Assert.Equal(WindowGeom.MixedSeed("geo-1"), WindowGeom.MixedSeed("geo-1"));
        Assert.NotEqual(WindowGeom.MixedSeed("1"), WindowGeom.MixedSeed("01x"));
        // a leading zero is still the number; past 2^64-1 it is a string
        Assert.Equal(WindowGeom.MixedSeed("1"), WindowGeom.MixedSeed("01"));
        Assert.Equal(WindowGeom.MixedSeed("18446744073709551616"),
            WindowGeom.MixedSeed("18446744073709551616"));
        Assert.NotEqual(WindowGeom.MixedSeed("18446744073709551615"), WindowGeom.MixedSeed("18446744073709551616"));
    }

    [Fact]
    public void DerivationIsDeterministicAndSeedsDiffer()
    {
        var a = WindowGeom.DeriveWindow(WindowGeom.MixedSeed("7"), 1920, 1080, 1920, 1032);
        Assert.Equal(a, WindowGeom.DeriveWindow(WindowGeom.MixedSeed("7"), 1920, 1080, 1920, 1032));
        var seen = Enumerable.Range(0, 200)
            .Select(i => WindowGeom.DeriveWindow(WindowGeom.MixedSeed(i.ToString()), 1920, 1080, 1920, 1032))
            .ToHashSet();
        Assert.True(seen.Count > 70, $"{seen.Count} distinct windows of 200");
    }

    [Fact]
    public void TooSmallOrInconsistentWorkAreasGetNoWindow()
    {
        Assert.False(WindowGeom.DeriveWindow(1, 800, 600, 800, 500).Valid);     // shorter than a desktop's
        Assert.False(WindowGeom.DeriveWindow(1, 1024, 768, 640, 768).Valid);    // narrower than a desktop's
        Assert.False(WindowGeom.DeriveWindow(1, 1366, 768, 1400, 728).Valid);   // avail wider than screen
    }

    [Theory]
    [InlineData(1366, 768, 1366, 728, "windows")]
    [InlineData(1366, 768, 1366, 728, "linux")]
    [InlineData(1920, 1080, 1920, 1032, "windows")]
    [InlineData(1920, 1080, 1920, 1032, "linux")]
    [InlineData(1920, 1080, 1920, 1080, "windows")]
    [InlineData(1920, 1080, 1920, 1080, "linux")]
    [InlineData(2560, 1440, 2560, 1392, "windows")]
    [InlineData(2560, 1440, 2560, 1392, "linux")]
    [InlineData(3440, 1440, 3440, 1392, "windows")]
    [InlineData(3440, 1440, 3440, 1392, "linux")]
    [InlineData(3840, 2160, 3840, 2112, "windows")]
    [InlineData(3840, 2160, 3840, 2112, "linux")]
    public void DistributionOver2000Seeds(int sw, int sh, int aw, int ah, string platform)
    {
        // The release-gate shape, per screen: every window inside the work area, maximized the common
        // state (40-75 %), no restored size on more than 1 % of identities, snapped halves Windows-only.
        const int n = 2000;
        var maximized = 0;
        var sizes = new Dictionary<(int, int), int>();
        for (var i = 1; i <= n; i++)
        {
            var g = WindowGeom.DeriveWindow(WindowGeom.MixedSeed(i.ToString()), sw, sh, aw, ah, platform);
            Assert.True(g.Valid);
            Assert.True(g.X >= 0 && g.Y >= 0 && g.X + g.Width <= aw && g.Y + g.Height <= ah, g.ToString());
            var snap = g.Kind!.StartsWith("snap", StringComparison.Ordinal);
            Assert.True(!snap || platform == "windows", g.ToString());
            if (g.State == "maximized") maximized++;
            else if (!snap)
            {
                sizes[(g.Width, g.Height)] = sizes.GetValueOrDefault((g.Width, g.Height)) + 1;
                Assert.True(g.Width >= Math.Min(aw, 900) && g.Height >= Math.Min(ah, 600), g.ToString());
            }
        }
        var share = maximized / (double)n;
        Assert.InRange(share, 0.40, 0.75);
        Assert.True(sizes.Values.Max() <= n / 100, $"one size on {sizes.Values.Max()} of {n} seeds");
        // Chrome's own fresh-profile box (10,10 + half the work area minus 15) is a fleet tell, never drawn
        Assert.False(sizes.ContainsKey((aw / 2 - 15, ah - 20)));
    }

    // ─────────────────────────────────────────────────────── the engine probe
    /// A stand-in engine binary: the probe looks for the NUL-delimited switch literal (in chrome.dll next to
    /// the executable on Windows). Not named chrome(.exe): an install check of a launch would want a whole
    /// browser directory around it.
    private string Engine(bool withSwitch = true)
    {
        var d = TestTemp.Create("cc-r39-window-");
        _dirs.Add(d);
        var body = Encoding.Latin1.GetBytes(withSwitch ? "\0junk\0fingerprint-window\0" : "\0junk\0fingerprint\0");
        var exe = Path.Combine(d, "fake-chrome");
        File.WriteAllBytes(exe, body);
        if (Native.IsWindows) File.WriteAllBytes(Path.Combine(d, "chrome.dll"), body);
        return exe;
    }

    [Fact]
    public void EnginePlacesPersonaWindowNeedsAPersonaTheSwitchAndTheEnginesRule()
    {
        var r39 = Engine();
        var r38 = Engine(withSwitch: false);
        Assert.True(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7" }));
        Assert.True(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--persona-from-env" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r38, new[] { "--fingerprint=7" }));   // r38 and older
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, Array.Empty<string>()));          // no persona
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, null));
        Assert.False(Geometry.EnginePlacesPersonaWindow(null, new[] { "--fingerprint=7" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-window=off" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-window=OFF" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-window=0" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-platform=android" }));
        // a schema 1-3 pin keeps the old window unless the caller turns it on
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-schema=3" }));
        Assert.False(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-schema=1", "--fingerprint-window=auto" }));
        Assert.True(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-schema=3", "--fingerprint-window=on" }));
        Assert.True(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-schema=4" }));
        // the last value wins, as in the engine's command line
        Assert.True(Geometry.EnginePlacesPersonaWindow(r39, new[] { "--fingerprint=7", "--fingerprint-window=off", "--fingerprint-window=on" }));
        // the switch literal is what is probed: "--fingerprint" alone is not "--fingerprint-window"
        Assert.False(LaunchOpts.EngineSupportsSwitch(r38, Geometry.PersonaWindowSwitch));
        Assert.True(LaunchOpts.EngineSupportsSwitch(r39, Geometry.PersonaWindowSwitch));
    }

    // ─────────────────────────────────────────────────────── the launch plan
    /// The context gets Playwright's NoViewport (the real window).
    private static void AssertNoViewport(ViewportSize? viewport)
    {
        Assert.NotNull(viewport);
        Assert.False(Geometry.IsExplicitViewport(viewport));
    }

    /// Two plans that are the same plan (a record compares its Args array by reference).
    private static void AssertSamePlan(Geometry.HeadlessPlan expected, Geometry.HeadlessPlan actual)
    {
        Assert.Equal(expected.Mode, actual.Mode);
        Assert.Equal(expected.Display, actual.Display);
        Assert.Equal(expected.Args, actual.Args);
        Assert.Equal(expected.Reconcile, actual.Reconcile);
    }

    [Theory]
    [InlineData(null)]
    [InlineData(true)]
    public void NativePersonaPlanSendsNoDisplayAndNoFit(bool? headless)
    {
        var plan = Geometry.ResolveHeadless(headless, "7", new[] { "--fingerprint=7" }, callerSetGeometry: false, exe: Engine());
        Assert.Equal(Geometry.Mode.PersonaNative, plan.Mode);
        Assert.Null(plan.Display);
        Assert.Empty(plan.Args);
        Assert.False(plan.Reconcile);
        // the caller chose nothing: the context takes the real window
        AssertNoViewport(Geometry.ContextViewport(plan, headless, null, null));
    }

    [Fact]
    public void NativePersonaPlanKeepsExplicitChoicesUntouched()
    {
        var exe = Engine();
        var args = new[] { "--fingerprint=7" };
        // NoViewport (the SC 203 configuration): the engine does it all, nothing to reconcile
        var none = ViewportSize.NoViewport;
        var p1 = Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: true, exe: exe,
            callerViewport: Geometry.IsExplicitViewport(none));
        Assert.Equal((Geometry.Mode.PersonaNative, false), (p1.Mode, p1.Reconcile));
        Assert.Same(none, Geometry.ContextViewport(p1, true, none, null));
        // an explicit viewport is kept as asked and only gets the position reconcile
        var vp = new ViewportSize { Width = 1280, Height = 720 };
        var p2 = Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: true, exe: exe,
            callerViewport: Geometry.IsExplicitViewport(vp));
        Assert.Equal((Geometry.Mode.PersonaNative, true), (p2.Mode, p2.Reconcile));
        Assert.Empty(p2.Args);
        Assert.Same(vp, Geometry.ContextViewport(p2, true, vp, null));
        // a ScreenSize alone: no NoViewport default is forced on the caller, nothing to reconcile
        var screen = new ScreenSize { Width = 1280, Height = 720 };
        var p3 = Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: true, exe: exe, callerViewport: false);
        Assert.Equal((Geometry.Mode.PersonaNative, false), (p3.Mode, p3.Reconcile));
        Assert.Null(Geometry.ContextViewport(p3, true, null, screen));
    }

    [Fact]
    public void IsExplicitViewportTellsAViewportFromNoViewport()
    {
        Assert.False(Geometry.IsExplicitViewport(null));
        Assert.False(Geometry.IsExplicitViewport(ViewportSize.NoViewport));
        Assert.False(Geometry.IsExplicitViewport(new ViewportSize
            { Width = ViewportSize.NoViewport.Width, Height = ViewportSize.NoViewport.Height }));
        Assert.True(Geometry.IsExplicitViewport(new ViewportSize { Width = 1280, Height = 720 }));
    }

    [Fact]
    public void AnOlderEngineKeepsTheOldPlan()
    {
        var r38 = Engine(withSwitch: false);
        var args = new[] { "--fingerprint=7" };
        var plan = Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: false, exe: r38);
        AssertSamePlan(Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: false), plan);
        Assert.Equal(Geometry.Mode.Persona, plan.Mode);
        Assert.StartsWith("--screen-info=", Assert.Single(plan.Args));
        AssertNoViewport(Geometry.ContextViewport(plan, true, null, null));
        // and a caller's own geometry still opts out entirely
        var own = Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: true, exe: r38, callerViewport: true);
        Assert.Equal(Geometry.Mode.None, own.Mode);
        Assert.Empty(own.Args);
        Assert.Null(Geometry.ContextViewport(own, true, null, new ScreenSize { Width = 1280, Height = 720 }));
    }

    [Fact]
    public void SeedlessAndSchemaPinnedLaunchesOnAnR39EngineKeepTheOldPlan()
    {
        var r39 = Engine();
        foreach (var args in new[]
                 {
                     new[] { "--no-sandbox" },                                       // seedless (regime 2)
                     new[] { "--fingerprint=7", "--fingerprint-schema=3" },          // schema pin
                     new[] { "--fingerprint=7", "--fingerprint-window=off" },
                 })
            AssertSamePlan(Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: false),
                Geometry.ResolveHeadless(true, "7", args, callerSetGeometry: false, exe: r39));
    }

    [Fact]
    public void HeadedIsLeftAlone()
    {
        var plan = Geometry.ResolveHeadless(false, "7", new[] { "--fingerprint=7" }, callerSetGeometry: false, exe: Engine());
        Assert.Equal(Geometry.Mode.None, plan.Mode);
        Assert.Empty(plan.Args);
        AssertNoViewport(Geometry.ContextViewport(plan, false, null, null));
    }

    [Fact]
    public void HeadedLinuxFitIsSkippedForAnR39Persona()
    {
        var r39 = Engine();
        Assert.False(Geometry.HeadedLinuxFit(new[] { "--fingerprint=7" }, true, r39));
        Assert.True(Geometry.HeadedLinuxFit(Array.Empty<string>(), true, r39));                  // no persona
        Assert.True(Geometry.HeadedLinuxFit(new[] { "--fingerprint=7" }, true, Engine(false)));  // r38
        Assert.True(Geometry.HeadedLinuxFit(new[] { "--fingerprint=7" }, linuxHost: true));       // no engine given
    }

    [Theory]
    [InlineData(0, 0, 1296, 854, "normal", null, null)]
    [InlineData(900, 300, 1296, 854, "normal", 624, 178)]
    [InlineData(40, 20, 2000, 900, "normal", 0, 20)]
    [InlineData(0, 0, 1920, 1032, "maximized", null, null)]
    [InlineData(900, 300, 1296, 854, null, 624, 178)]
    public void ReconcileOnlyMovesAWindowBackOntoTheWorkArea(int left, int top, int w, int h, string? state, int? wantLeft, int? wantTop)
    {
        var plan = Geometry.ReconcilePlan(left, top, w, h, state, new[] { 1920, 1032 });
        (int Left, int Top)? want = wantLeft is null ? null : (wantLeft.Value, wantTop!.Value);
        Assert.Equal(want, plan);
    }

    // ─────────────────────────────────────────────────────── the first-page window step, on a fake page
    /// A page whose window is a CDP window: the work area, the window's bounds, every CDP call recorded.
    public class FakePage : DispatchProxy
    {
        public int[] Avail = { 1920, 1032 };
        public Dictionary<string, object> WindowBounds = new()
        {
            ["left"] = 0, ["top"] = 0, ["width"] = 1920, ["height"] = 1032, ["windowState"] = "normal",
        };
        public object? Context;
        public readonly List<(string Method, string Params)> Calls = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "get_Context") return Context;
            if (method is { Name: "EvaluateAsync", IsGenericMethod: true })
            {
                var js = (string)args![0]!;
                return Task.FromResult(js.Contains("availWidth")
                    ? Avail
                    : new[] { Convert.ToInt32(WindowBounds["width"]), Convert.ToInt32(WindowBounds["height"]) });
            }
            throw new InvalidOperationException(method?.Name);
        }
    }

    public class FakeCdp : DispatchProxy
    {
        public FakePage? Page;

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            var name = (string)args![0]!;
            Page!.Calls.Add((name, JsonSerializer.Serialize(args.Length > 1 ? args[1] : null)));
            if (name == "Browser.setWindowBounds")
                foreach (var (k, v) in (Dictionary<string, object>)((Dictionary<string, object>)args[1]!)["bounds"])
                    Page.WindowBounds[k] = v;
            var json = name == "Browser.getWindowForTarget"
                ? JsonSerializer.Serialize(new { windowId = 3, bounds = Page.WindowBounds })
                : "{}";
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());
        }
    }

    public class FakeContext : DispatchProxy
    {
        public ICDPSession? Cdp;
        public IPage? Page;

        protected override object? Invoke(MethodInfo? method, object?[]? args) => method?.Name switch
        {
            "NewCDPSessionAsync" => Task.FromResult(Cdp!),
            "get_Pages" => (IReadOnlyList<IPage>)new[] { Page! },
            _ => throw new InvalidOperationException(method?.Name),
        };
    }

    private static (IBrowserContext Context, FakePage Page) FakeWindow(int left, int top, int width, int height)
    {
        var page = DispatchProxy.Create<IPage, FakePage>();
        var ctx = DispatchProxy.Create<IBrowserContext, FakeContext>();
        var cdp = DispatchProxy.Create<ICDPSession, FakeCdp>();
        var fake = (FakePage)(object)page;
        fake.Context = ctx;
        fake.WindowBounds = new() { ["left"] = left, ["top"] = top, ["width"] = width, ["height"] = height, ["windowState"] = "normal" };
        ((FakeContext)(object)ctx).Cdp = cdp;
        ((FakeContext)(object)ctx).Page = page;
        ((FakeCdp)(object)cdp).Page = fake;
        return (ctx, fake);
    }

    [Fact]
    public async Task ANativePersonaWindowGetsNoCdpCallsWithoutAViewport()
    {
        var (ctx, fake) = FakeWindow(900, 300, 1296, 854);
        var plan = Geometry.ResolveHeadless(true, "7", new[] { "--fingerprint=7" }, callerSetGeometry: false, exe: Engine());
        await Geometry.InstallWindowFixupAsync(ctx, new[] { "--fingerprint=7" }, plan);
        Assert.Empty(fake.Calls);   // not even a read: the engine placed the window
    }

    [Fact]
    public async Task ANativePersonaWindowUnderAnExplicitViewportIsOnlyMovedBackOn()
    {
        var (ctx, fake) = FakeWindow(900, 300, 1296, 854);
        var plan = Geometry.ResolveHeadless(true, "7", new[] { "--fingerprint=7" }, callerSetGeometry: true, exe: Engine(),
            callerViewport: true);
        await Geometry.InstallWindowFixupAsync(ctx, new[] { "--fingerprint=7" }, plan);
        var set = Assert.Single(fake.Calls, c => c.Method == "Browser.setWindowBounds");
        Assert.Equal("{\"windowId\":3,\"bounds\":{\"left\":624,\"top\":178}}", set.Params);   // left/top only, never a size
        // already on the work area: nothing to move
        var (ctx2, fake2) = FakeWindow(0, 0, 1296, 854);
        await Geometry.InstallWindowFixupAsync(ctx2, new[] { "--fingerprint=7" }, plan);
        Assert.DoesNotContain(fake2.Calls, c => c.Method == "Browser.setWindowBounds");
    }

    [Fact]
    public async Task AnOlderEnginesPersonaWindowStillGetsTheWorkAreaFit()
    {
        var (ctx, fake) = FakeWindow(10, 10, 945, 1020);
        var plan = Geometry.ResolveHeadless(true, "7", new[] { "--fingerprint=7" }, callerSetGeometry: false, exe: Engine(false));
        await Geometry.InstallWindowFixupAsync(ctx, new[] { "--fingerprint=7" }, plan);
        var sets = fake.Calls.Where(c => c.Method == "Browser.setWindowBounds").Select(c => c.Params).ToList();
        Assert.NotEmpty(sets);   // r38 and older engines keep the work-area fit
        Assert.Contains("\"width\":1920", sets[0]);
    }

    // ─────────────────────────────────────────────────────── ServeAsync
    [Fact]
    public void ServedNativePersonaGetsNothingButAnExplicitWindowSize()
    {
        var r39 = Engine();
        var args = new[] { "--fingerprint=7" };
        var g = Geometry.ServedGeometry(args, "7", lightStealth: false, headless: true, exe: r39)!;
        Assert.Equal((true, true, false), (g.Persona, g.Native, g.Fit));
        Assert.Null(g.Display);
        Assert.Empty(g.Args);
        var size = new ViewportSize { Width = 1200, Height = 800 };
        Assert.True(Geometry.ServedGeometry(args, "7", false, true, exe: r39, windowSize: size)!.Fit);
        // a caller's own --window-position keeps its place even with a size
        var placed = Geometry.ServedGeometry(new[] { "--fingerprint=7", "--window-position=5,5" }, "7", false, true, exe: r39, windowSize: size)!;
        Assert.Equal((true, false), (placed.Native, placed.Fit));
        Assert.Empty(placed.Args);
        // a caller's own size or display, or headed, still leaves it all alone
        Assert.Null(Geometry.ServedGeometry(new[] { "--fingerprint=7", "--window-size=1200,800" }, "7", false, true, exe: r39));
        Assert.Null(Geometry.ServedGeometry(new[] { "--fingerprint=7", "--screen-info={1280x720}" }, "7", false, true, exe: r39));
        Assert.Null(Geometry.ServedGeometry(args, "7", false, headless: false, exe: r39));
    }

    [Fact]
    public void ServedOnAnOlderEngineOrSeedlessKeepsTheDisplayAndTheFit()
    {
        var r38 = Engine(withSwitch: false);
        var old = Geometry.ServedGeometry(new[] { "--fingerprint=7" }, "7", false, true, exe: r38,
            windowSize: new ViewportSize { Width = 1200, Height = 800 })!;
        Assert.Equal(Geometry.ServedGeometry(new[] { "--fingerprint=7" }, "7", false, true)!.Args, old.Args);
        Assert.False(old.Native);
        Assert.True(old.Fit);
        Assert.StartsWith("--screen-info=", old.Args[0]);
        var seedless = Geometry.ServedGeometry(new[] { "--no-sandbox" }, null, false, true, exe: Engine())!;
        Assert.False(seedless.Native);
        Assert.Equal(new[] { Geometry.ScreenInfoSwitch(seedless.Display!), "--window-position=0,0" }, seedless.Args);
    }

    /// A served browser over CDP, for the native fit: the display read, the window's bounds and state.
    private sealed class FakeServed : Geometry.ICdpSend
    {
        public readonly Dictionary<string, object> Bounds;
        public readonly List<(string Method, JsonElement Params)> Calls = new();

        public FakeServed(int left, int top, int width, int height, string state) =>
            Bounds = new() { ["left"] = left, ["top"] = top, ["width"] = width, ["height"] = height, ["windowState"] = state };

        private static JsonElement J(object o) => JsonSerializer.SerializeToElement(o);

        public Task<JsonElement> SendAsync(string method, object? parameters = null, string? sessionId = null)
        {
            var p = J(parameters ?? new Dictionary<string, object>());
            Calls.Add((method, p));
            switch (method)
            {
                case "Target.getTargets": return Task.FromResult(J(new { targetInfos = new[] { new { targetId = "T", type = "page" } } }));
                case "Target.attachToTarget": return Task.FromResult(J(new { sessionId = "S" }));
                case "Emulation.getScreenInfos": return Task.FromResult(J(new { screenInfos = new[] { new { id = "1", isPrimary = true } } }));
                case "Runtime.evaluate":
                    return Task.FromResult(p.GetProperty("expression").GetString()!.Contains("availLeft")
                        ? J(new { result = new { value = new[] { 1920, 1080, 0, 0, 1920, 1032 } } })
                        : J(new { result = new { value = new[] { Bounds["width"], Bounds["height"] } } }));
                case "Browser.getWindowForTarget": return Task.FromResult(J(new { windowId = 3, bounds = new Dictionary<string, object>(Bounds) }));
                case "Browser.setWindowBounds":
                {
                    var b = p.GetProperty("bounds");
                    if (b.TryGetProperty("width", out var w))
                    {
                        Bounds["width"] = w.GetInt32();
                        Bounds["height"] = b.GetProperty("height").GetInt32();
                        Bounds["left"] = b.GetProperty("left").GetInt32();
                        Bounds["top"] = b.GetProperty("top").GetInt32();
                    }
                    Bounds["windowState"] = b.TryGetProperty("windowState", out var s) ? s.GetString()! : "normal";
                    return Task.FromResult(J(new { }));
                }
                default: return Task.FromResult(J(new { }));
            }
        }

        public string[] Methods => Calls.Select(c => c.Method).ToArray();
        public string[] Sets => Calls.Where(c => c.Method == "Browser.setWindowBounds").Select(c => c.Params.GetProperty("bounds").GetRawText()).ToArray();
    }

    [Fact]
    public async Task TheNativeServedFitDoesNothingWithoutAWindowSize()
    {
        var f = new FakeServed(120, 30, 1400, 900, "normal");
        Assert.Null(await Geometry.FitWindowOverCdpAsync(f, persona: true, windowSize: null, native: true));
        Assert.DoesNotContain("Emulation.updateScreen", f.Methods);
        Assert.DoesNotContain("Browser.setWindowBounds", f.Methods);
        Assert.Equal("Target.detachFromTarget", f.Methods[^1]);
    }

    [Fact]
    public async Task TheNativeServedFitKeepsThePersonasPosition()
    {
        var f = new FakeServed(900, 30, 1400, 900, "normal");
        var r = await Geometry.FitWindowOverCdpAsync(f, persona: true, new ViewportSize { Width = 1200, Height = 800 }, settleMs: 50, native: true);
        Assert.Equal(new[] { 1200, 800 }, r!.Value.Outer);
        Assert.Equal(new Geometry.Display(1920, 1080, 1920, 1032), r.Value.Display);
        Assert.DoesNotContain("Emulation.updateScreen", f.Methods);   // the display already is the persona's
        Assert.DoesNotContain("Emulation.getScreenInfos", f.Methods);
        Assert.Equal("{\"left\":720,\"top\":30,\"width\":1200,\"height\":800}", f.Sets[^1]);   // moved just enough
    }

    [Fact]
    public async Task TheNativeServedFitRestoresAMaximizedWindowBeforeResizingIt()
    {
        var f = new FakeServed(0, 0, 1920, 1032, "maximized");
        var r = await Geometry.FitWindowOverCdpAsync(f, persona: true, new ViewportSize { Width = 1200, Height = 800 }, settleMs: 50, native: true);
        Assert.Equal("{\"windowState\":\"normal\"}", f.Sets[0]);
        Assert.Equal("{\"left\":0,\"top\":0,\"width\":1200,\"height\":800}", f.Sets[^1]);
        Assert.Equal(new[] { 1200, 800 }, r!.Value.Outer);
    }

    [Fact]
    public async Task TheServedFitOnAnOlderEngineStillSetsThePersonasDisplay()
    {
        // native: false is the r38 path, byte for byte: updateScreen, then the fit from (10,10).
        var f = new FakeServed(10, 10, 780, 580, "normal");
        await Geometry.FitWindowOverCdpAsync(f, persona: true, new ViewportSize { Width = 1200, Height = 800 }, settleMs: 50);
        Assert.Contains("Emulation.updateScreen", f.Methods);
        Assert.Equal("{\"left\":10,\"top\":10,\"width\":1200,\"height\":800}", f.Sets[0]);
    }
}
