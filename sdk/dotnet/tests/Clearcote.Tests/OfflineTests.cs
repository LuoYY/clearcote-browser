using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Offline mode: an Offline Key file replaces every licence call (no checkout, heartbeat, check-in or download);
// its token goes to the engine as CLEARCOTE_RUN_TOKEN and the binary must be given.
public class OfflineTests
{
    private static readonly DateTimeOffset Now = new(2026, 10, 16, 12, 0, 0, TimeSpan.Zero);

    private static string KeyFile(Action<Dictionary<string, object>>? edit = null)
    {
        var data = new Dictionary<string, object>
        {
            ["format"] = "clearcote-offline-key",
            ["v"] = 1,
            ["licence_id"] = "lic_1",
            ["expires_at"] = Now.AddDays(40).ToString("O"),
            ["token"] = "eyJwYXlsb2FkIjoxfQ.c2ln",
        };
        edit?.Invoke(data);
        var path = Path.Combine(Path.GetTempPath(), $"cc-offline-{Guid.NewGuid():N}.json");
        File.WriteAllText(path, JsonSerializer.Serialize(data));
        return path;
    }

    [Fact]
    public void NoKeyMeansOnlineMode()
    {
        Assert.Null(Offline.OfflineKeyPath(null, ""));
        Assert.Null(Offline.Lease(null, now: Now, envValue: ""));
    }

    [Fact]
    public async Task AValidKeyGivesItsTokenAndAnInertLease()
    {
        var lease = Offline.Lease(KeyFile(), now: Now)!;
        Assert.Equal("eyJwYXlsb2FkIjoxfQ.c2ln", lease.Token);
        Assert.Equal("offline", lease.LeaseId);
        Assert.False(await lease.RefreshTokenAsync());
        await lease.StopAsync();
        var env = License.WithRunToken(lease.Token, new Dictionary<string, string>(), lease.BindLaunch().File);
        Assert.Equal("eyJwYXlsb2FkIjoxfQ.c2ln", env["CLEARCOTE_RUN_TOKEN"]);
        Assert.False(env.ContainsKey("CLEARCOTE_RUN_TOKEN_FILE"));
    }

    [Theory]
    [InlineData("expired", "expired")]
    [InlineData("format", "not a Clearcote offline key")]
    [InlineData("token", "no valid token")]
    [InlineData("expires", "no valid expires_at")]
    public void BadKeysFailClearly(string which, string message)
    {
        var path = KeyFile(d =>
        {
            switch (which)
            {
                case "expired": d["expires_at"] = Now.AddDays(-1).ToString("O"); break;
                case "format": d["format"] = "something-else"; break;
                case "token": d["token"] = "no-dot"; break;
                case "expires": d["expires_at"] = "soon"; break;
            }
        });
        var e = Assert.Throws<OfflineKeyError>(() => Offline.Lease(path, now: Now));
        Assert.Contains(message, e.Message);
    }

    [Fact]
    public void MissingAndUnreadableFiles()
    {
        Assert.Throws<OfflineKeyError>(() => Offline.LoadOfflineKey(Path.Combine(Path.GetTempPath(), "nope-cc-offline.json"), Now));
        var bad = Path.Combine(Path.GetTempPath(), $"cc-offline-{Guid.NewGuid():N}.json");
        File.WriteAllText(bad, "{not json");
        Assert.Contains("JSON", Assert.Throws<OfflineKeyError>(() => Offline.LoadOfflineKey(bad, Now)).Message);
    }

    [Fact]
    public void OfflineModeNeedsABinary()
    {
        Assert.Throws<OfflineKeyError>(() => Offline.RequireOfflineBinary("/k.json", null, envKey: "", envBinary: ""));
        Assert.Throws<OfflineKeyError>(() => Offline.RequireOfflineBinary(null, null, envKey: "/k.json", envBinary: ""));
        Offline.RequireOfflineBinary("/k.json", "/opt/clearcote/chrome", envKey: "", envBinary: "");
        Offline.RequireOfflineBinary("/k.json", null, envKey: "", envBinary: "/opt/clearcote/chrome");
        Offline.RequireOfflineBinary(null, null, envKey: "", envBinary: ""); // online mode: nothing to check
    }

    [Fact]
    public async Task AcquireLeaseWithAnOfflineKeyMakesNoNetworkCall()
    {
        var key = KeyFile(d => d["expires_at"] = DateTimeOffset.UtcNow.AddDays(40).ToString("O"));
        // An unreachable backend: an online checkout would fail; the offline path never gets there.
        var lease = await License.AcquireLeaseAsync(new LicenseOptions
        {
            LicenseKey = "cc_lic_" + new string('x', 40),
            LicenseApiBase = "http://127.0.0.1:9",
            OfflineKey = key,
        }, quiet: true);
        Assert.NotNull(lease);
        Assert.Equal("eyJwYXlsb2FkIjoxfQ.c2ln", lease!.Token);
    }
}
