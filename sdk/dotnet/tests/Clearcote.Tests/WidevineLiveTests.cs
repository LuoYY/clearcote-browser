using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Live: Widevine ON by default for a licensed (PRO) launch, against a real engine. Skipped unless
/// CLEARCOTE_LIVE_ENGINE points at a PRO chrome binary (r32+, it has --widevine-cdm-path) AND a licence key resolves
/// (CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key). Downloads Google's CDM from Google into a temp cache once, so
/// it needs the network. Headless. These belong in the release gate. Mirrors the Python test_widevine_live.py.
/// </summary>
public class WidevineLiveTests : IDisposable
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
    internal static bool Run => !string.IsNullOrEmpty(LiveExe) && License.ResolveLicenseKey() is not null;

    private const string Eme = @"async () => { try {
  const a = await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{initDataTypes: ['cenc'],
    videoCapabilities: [{contentType: 'video/mp4; codecs=""avc1.42E01E""'}]}]);
  await a.createMediaKeys(); return 'ok ' + a.keySystem;
} catch (e) { return e.name; } }";

    private readonly Sandbox _sb = new();
    private readonly string _root = TestTemp.Create("cc-wv-live-");
    private readonly TcpListener _http = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _stop = new();

    public WidevineLiveTests()
    {
        _sb.Env("CLEARCOTE_WIDEVINE", null).Env("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "120")
            .Env("CLEARCOTE_WIDEVINE_DIR", Path.Combine(_root, "cache"));   // EMPTY: the first default launch downloads it
        Widevine.ResetDefaultState();
        // EME needs a secure context: http://127.0.0.1 is one. A one-page server.
        _http.Start();
        _ = Task.Run(async () =>
        {
            var body = Encoding.ASCII.GetBytes("<!doctype html><title>eme</title><p>eme");
            while (!_stop.IsCancellationRequested)
            {
                try
                {
                    using var c = await _http.AcceptTcpClientAsync(_stop.Token);
                    var s = c.GetStream();
                    var buf = new byte[4096];
                    await s.ReadAsync(buf, _stop.Token);
                    await s.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"));
                    await s.WriteAsync(body);
                }
                catch { /* stopped, or a client gave up */ }
            }
        });
    }

    public void Dispose()
    {
        _stop.Cancel();
        _http.Stop();
        Widevine.ResetDefaultState();
        _sb.Dispose();
        TestTemp.Remove(_root);
    }

    private string Url => $"http://127.0.0.1:{((IPEndPoint)_http.LocalEndpoint).Port}/";

    private async Task<string> EmeAsync(IPage page)
    {
        await page.GotoAsync(Url);
        return await page.EvaluateAsync<string>(Eme);
    }

    [WidevineLiveFact]
    public async Task Default_licensed_launches_have_widevine_and_opting_out_has_not()
    {
        var cache = Path.Combine(_root, "cache");
        Assert.Null(Widevine.CachedCdm(cache));

        var udd = Path.Combine(_root, "udd");
        var ctx = await Clearcote.LaunchPersistentContextAsync(udd, new LaunchOptions { ExecutablePath = LiveExe, Headless = true, Quiet = true });
        try { Assert.Equal("ok com.widevine.alpha", await EmeAsync(await ctx.NewPageAsync())); }
        finally { await ctx.CloseAsync(); }
        Assert.NotNull(Widevine.CachedCdm(cache));   // downloaded, verified, installed
        Assert.DoesNotContain(Directory.EnumerateFileSystemEntries(cache).Select(Path.GetFileName),
            n => n!.StartsWith(".tmp-") || n.StartsWith(".trash-") || n.StartsWith(".install-lock"));
        Assert.False(Directory.Exists(Path.Combine(udd, "WidevineCdm")));   // the default never seeds the profile

        // the incognito launch and the ephemeral profile, from the cache
        var browser = await Clearcote.LaunchAsync(new LaunchOptions { ExecutablePath = LiveExe, Headless = true, Quiet = true });
        try { Assert.Equal("ok com.widevine.alpha", await EmeAsync(await browser.NewPageAsync())); }
        finally { await browser.CloseAsync(); }
        var eph = await Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions { ExecutablePath = LiveExe, Headless = true, Quiet = true });
        try { Assert.Equal("ok com.widevine.alpha", await EmeAsync(await eph.NewPageAsync())); }
        finally { await eph.CloseAsync(); }

        // opted out: the option, then the environment
        var off = await Clearcote.LaunchPersistentContextAsync(Path.Combine(_root, "udd-off"),
            new LaunchOptions { ExecutablePath = LiveExe, Headless = true, Quiet = true, Widevine = false });
        try { Assert.Equal("NotSupportedError", await EmeAsync(await off.NewPageAsync())); }
        finally { await off.CloseAsync(); }
        _sb.Env("CLEARCOTE_WIDEVINE", "0");
        var offEnv = await Clearcote.LaunchAsync(new LaunchOptions { ExecutablePath = LiveExe, Headless = true, Quiet = true });
        try { Assert.Equal("NotSupportedError", await EmeAsync(await offEnv.NewPageAsync())); }
        finally { await offEnv.CloseAsync(); }
    }
}

/// Runs the live test only when it is configured, and otherwise reports it as skipped, with the reason, instead
/// of passing without having run.
public sealed class WidevineLiveFactAttribute : FactAttribute
{
    public WidevineLiveFactAttribute()
    {
        if (!WidevineLiveTests.Run)
            Skip = "not configured: set CLEARCOTE_LIVE_ENGINE to a PRO engine (r32+) and a licence key (CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key)";
    }
}
