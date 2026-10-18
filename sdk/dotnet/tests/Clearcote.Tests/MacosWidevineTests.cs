using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Widevine on a Mac: the CDM a Mac asks Google for, and that it lands where a Mac browser loads it. Mirrors the
/// Node suite's test/widevine-macos.test.ts and the Python suite's tests/test_widevine_macos.py.
/// </summary>
/// <remarks>
/// Before this, <c>CdmPlatform</c> knew Linux and sent everything else down the Windows branch, so the native macOS
/// PRO build (an Apple Silicon Chromium.app) was handed widevinecdm.dll under win_x64 — a library no Mac can load,
/// seeded into every profile. The coordinates here are what Google's server really answers (2026-10-17: an arm64
/// Mac gets <c>oimompecagnajdejgnnjijobebaeigek_4.10.3112.0_mac_arm64_….crx3</c>, whose zip holds
/// _platform_specific/mac_arm64/libwidevinecdm.dylib). There is no Mac in CI, so the OS and CPU are faked
/// (Native.OsTagOverride / ArchOverride, as MacosNativeTests does) and Google is a handler that records every request.
/// </remarks>
public class MacosWidevineTests : IDisposable
{
    private const string Version = "4.10.3112.0";
    private const string DownloadUrl = "https://dl.example.invalid/cdm.crx3";
    private readonly Sandbox _sb = new();
    private readonly string _tmp = TestTemp.Create("cc-wv-mac-");

    public MacosWidevineTests()
    {
        _sb.TempHome();
        _sb.Env("CLEARCOTE_WIDEVINE_DIR", Path.Combine(_tmp, "wv")).Env("CLEARCOTE_WIDEVINE", null);
        Widevine.ResetDefaultState();
    }

    public void Dispose()
    {
        Native.ArchOverride = null;
        Widevine.ResetDefaultState();
        _sb.Dispose();
        TestTemp.Remove(_tmp);
    }

    private void On(string os, string arch = "arm64")
    {
        _sb.Os(os);
        Native.ArchOverride = arch;
    }

    /// The macOS package's real layout (library + its .sig + manifest).
    private static byte[] MacCrx() => WidevineTests.Crx(new()
    {
        ["manifest.json"] = Encoding.ASCII.GetBytes($"{{\"version\":\"{Version}\"}}"),
        ["_platform_specific/mac_arm64/libwidevinecdm.dylib"] = Encoding.Latin1.GetBytes("\xcf\xfa\xed\xfe fake mach-o"),
        ["_platform_specific/mac_arm64/libwidevinecdm.dylib.sig"] = Encoding.ASCII.GetBytes("sig"),
    });

    /// Google's update server + download host, recording the update check's body and every User-Agent.
    private sealed class RecordingGoogle : HttpMessageHandler
    {
        public byte[] Blob = MacCrx();
        public readonly List<string> Bodies = new();
        public readonly List<string> Agents = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            var url = request.RequestUri!.ToString();
            lock (Agents) Agents.Add(request.Headers.UserAgent.ToString());
            if (url == Widevine.OmahaUrl)
            {
                var sent = await request.Content!.ReadAsStringAsync(ct);
                lock (Bodies) Bodies.Add(sent);
                var sha = Convert.ToHexString(SHA256.HashData(Blob)).ToLowerInvariant();
                // The classic shape Google really returns (codebase + package), XSSI guard included.
                var json = ")]}'\n{\"response\":{\"app\":[{\"updatecheck\":{\"status\":\"ok\",\"urls\":{\"url\":[{\"codebase\":\"https://dl.example.invalid/\"}]},"
                    + $"\"manifest\":{{\"version\":\"{Version}\",\"packages\":{{\"package\":[{{\"name\":\"cdm.crx3\",\"hash_sha256\":\"{sha}\"}}]}}}}}}}}]}}}}";
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(json) };
            }
            if (url == DownloadUrl) return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(Blob) };
            throw new InvalidOperationException($"unexpected request {url}");
        }
    }

    // ── coordinates ──────────────────────────────────────────────────────────

    [Fact]
    public void An_apple_silicon_mac_asks_for_the_mac_arm64_dylib()
    {
        On("macos", "arm64");
        Assert.Equal(("mac", "Mac OS X", "14.0.0", "mac_arm64", "libwidevinecdm.dylib"), Widevine.CdmPlatform());
        Assert.Equal(("arm64", "arm", "arm64"), Widevine.CdmArch());
    }

    [Fact]
    public void An_intel_mac_asks_for_the_mac_x64_dylib()
    {
        On("macos", "x64");
        Assert.Equal(("mac", "Mac OS X", "14.0.0", "mac_x64", "libwidevinecdm.dylib"), Widevine.CdmPlatform());
        Assert.Equal(("x64", "x86-64", "x86_64"), Widevine.CdmArch());
    }

    [Theory]
    [InlineData("x64")]
    [InlineData("arm64")]
    public void Windows_and_linux_are_what_they_were_whatever_the_cpu(string arch)
    {
        On("windows", arch);
        Assert.Equal(("win", "Windows", "10.0.19045.0", "win_x64", "widevinecdm.dll"), Widevine.CdmPlatform());
        Assert.Equal(("x64", "x86-64", "x86_64"), Widevine.CdmArch());
        On("linux", arch);
        Assert.Equal(("Linux", "Linux", "6.1.0", "linux_x64", "libwidevinecdm.so"), Widevine.CdmPlatform());
        Assert.Equal(("x64", "x86-64", "x86_64"), Widevine.CdmArch());
    }

    // ── the request ──────────────────────────────────────────────────────────

    /// The body the SDK sent before macOS existed, for Windows / Linux.
    private static string Before(string atOs, string platform, string version) =>
        "{\"request\":{\"@os\":\"" + atOs + "\",\"@updater\":\"clearcote\",\"acceptformat\":\"crx3\",\"protocol\":\"3.1\","
        + "\"arch\":\"x64\",\"nacl_arch\":\"x86-64\",\"prodversion\":\"149.0.0.0\",\"updaterversion\":\"149.0.0.0\",\"dedup\":\"cr\","
        + "\"os\":{\"arch\":\"x86_64\",\"platform\":\"" + platform + "\",\"version\":\"" + version + "\"},"
        + "\"app\":[{\"appid\":\"oimompecagnajdejgnnjijobebaeigek\",\"version\":\"0.0.0.0\",\"updatecheck\":{},\"ping\":{\"r\":-2}}]}}";

    private static string Compact(string json) => JsonSerializer.Serialize(JsonDocument.Parse(json).RootElement);

    [Fact]
    public void A_mac_asks_as_googles_own_mac_client_does()
    {
        On("macos", "arm64");
        using var doc = JsonDocument.Parse(Widevine.OmahaRequestBody());
        var r = doc.RootElement.GetProperty("request");
        Assert.Equal("mac", r.GetProperty("@os").GetString());
        Assert.Equal("arm64", r.GetProperty("arch").GetString());
        Assert.Equal("arm", r.GetProperty("nacl_arch").GetString());
        Assert.Equal("arm64", r.GetProperty("os").GetProperty("arch").GetString());
        Assert.Equal("Mac OS X", r.GetProperty("os").GetProperty("platform").GetString());
    }

    [Fact]
    public void Windows_and_linux_bodies_are_the_old_ones()
    {
        On("windows", "arm64");
        Assert.Equal(Compact(Before("win", "Windows", "10.0.19045.0")), Compact(Widevine.OmahaRequestBody()));
        On("linux", "x64");
        Assert.Equal(Compact(Before("Linux", "Linux", "6.1.0")), Compact(Widevine.OmahaRequestBody()));
    }

    [Fact]
    public void The_user_agent_names_the_os_asked_for()
    {
        On("macos");
        Assert.StartsWith("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", Widevine.UserAgent);
        On("windows");
        Assert.Contains("(Windows NT 10.0; Win64; x64)", Widevine.UserAgent);
        On("linux");
        Assert.Contains("(X11; Linux x86_64)", Widevine.UserAgent);
    }

    // ── end to end against a recording Google ────────────────────────────────

    [Fact]
    public async Task FetchAsync_on_a_mac_installs_the_dylib_with_a_mac_ua_on_every_request()
    {
        On("macos", "arm64");
        var g = new RecordingGoogle();
        _sb.Http(g);
        var dir = await Widevine.FetchAsync(quiet: true);
        var body = Assert.Single(g.Bodies);
        using (var doc = JsonDocument.Parse(body))
        {
            Assert.Equal("mac", doc.RootElement.GetProperty("request").GetProperty("@os").GetString());
            Assert.Equal("arm64", doc.RootElement.GetProperty("request").GetProperty("arch").GetString());
        }
        Assert.Equal(2, g.Agents.Count); // the check and the download
        Assert.All(g.Agents, ua => Assert.Contains("Macintosh", ua));
        Assert.True(File.Exists(Path.Combine(dir, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib")));
        Assert.True(File.Exists(Path.Combine(dir, "manifest.json")));
    }

    [Fact]
    public async Task A_package_without_the_mac_library_is_refused()
    {
        On("macos", "arm64");
        _sb.Http(new RecordingGoogle
        {
            Blob = WidevineTests.Crx(new()
            {
                ["manifest.json"] = Encoding.ASCII.GetBytes("{}"),
                ["_platform_specific/win_x64/widevinecdm.dll"] = Encoding.ASCII.GetBytes("MZ"),
            }),
        });
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Widevine.FetchAsync(quiet: true));
        Assert.Contains("libwidevinecdm.dylib", e.Message);
    }

    [Fact]
    public async Task SeedAsync_copies_the_mac_cdm_into_the_profile()
    {
        On("macos", "arm64");
        _sb.Http(new RecordingGoogle());
        var target = await Widevine.SeedAsync(Path.Combine(_tmp, "profile"), quiet: true);
        var lib = Path.Combine(target, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib");
        Assert.Contains("fake mach-o", Encoding.Latin1.GetString(File.ReadAllBytes(lib)));
    }

    [Theory]
    [InlineData("macos", true)]
    [InlineData("windows", true)]
    [InlineData("linux", false)]
    public void The_startup_scan_is_forced_on_mac_and_windows_not_linux(string os, bool scan)
    {
        On(os);
        var (_, args) = Widevine.ComponentUpdaterArgs(null, null);
        Assert.Equal(scan, args.Contains("--component-updater=fast-update"));
    }
}
