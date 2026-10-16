using System.Text.Json;

namespace Clearcote;

/// The Offline Key file is missing, malformed or expired, or offline mode lacks a binary.
public class OfflineKeyError : Exception
{
    public OfflineKeyError(string message) : base(message) { }
}

/// Offline mode: run the PRO engine with an Offline Key file, with no licence calls at all.
///
/// A Platform Licence holder downloads an Offline Key (a JSON file from the Licenses page, or
/// GET /api/v1/offline/key). Its <c>token</c> is a PRO run token the engine verifies locally (signature,
/// validity window and a rollback guard). With <see cref="LaunchOptions.OfflineKey"/> (or CLEARCOTE_OFFLINE_KEY)
/// set, a launch makes no lease checkout, heartbeat, check-in or download: the binary must be given
/// (<see cref="LaunchOptions.ExecutablePath"/> or CLEARCOTE_BINARY), and the key's token goes to the engine as
/// CLEARCOTE_RUN_TOKEN. The key is checked here first so an expired or malformed file fails with a clear message.
/// Mirrors sdk/python/clearcote/_offline.py and sdk/node/src/offline.ts.
public static class Offline
{
    public const string OfflineKeyEnv = "CLEARCOTE_OFFLINE_KEY";
    public const string OfflineKeyFormat = "clearcote-offline-key";
    private const int WarnDays = 7;

    /// explicit OfflineKey &gt; CLEARCOTE_OFFLINE_KEY. Null means online mode.
    public static string? OfflineKeyPath(string? @explicit = null, string? envValue = null)
    {
        if (!string.IsNullOrWhiteSpace(@explicit)) return @explicit.Trim();
        var v = (envValue ?? Environment.GetEnvironmentVariable(OfflineKeyEnv) ?? "").Trim();
        return v.Length > 0 ? v : null;
    }

    /// Read and check an Offline Key file: (token, expiry). Throws <see cref="OfflineKeyError"/>.
    public static (string Token, DateTimeOffset Expires) LoadOfflineKey(string path, DateTimeOffset? now = null)
    {
        string raw;
        try { raw = File.ReadAllText(path); }
        catch (Exception) { throw new OfflineKeyError($"Offline key file not found: {path}"); }
        JsonElement root;
        try { root = JsonDocument.Parse(raw).RootElement; }
        catch (JsonException e) { throw new OfflineKeyError($"Offline key file can't be read as JSON ({path}): {e.Message}"); }
        if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("format", out var f) ||
            f.ValueKind != JsonValueKind.String || f.GetString() != OfflineKeyFormat)
            throw new OfflineKeyError($"{path} is not a Clearcote offline key file.");
        if (!root.TryGetProperty("token", out var t) || t.ValueKind != JsonValueKind.String ||
            (t.GetString() ?? "").Split('.').Length != 2)
            throw new OfflineKeyError($"{path} has no valid token.");
        if (!root.TryGetProperty("expires_at", out var e2) || e2.ValueKind != JsonValueKind.String ||
            !DateTimeOffset.TryParse(e2.GetString(), System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal, out var expires))
            throw new OfflineKeyError($"{path} has no valid expires_at.");
        var at = now ?? DateTimeOffset.UtcNow;
        if (expires <= at)
            throw new OfflineKeyError(
                $"This offline key expired on {expires.UtcDateTime:yyyy-MM-dd}. Download the newest key from the " +
                "Offline card on your Licenses page (or GET /api/v1/offline/key) and install it.");
        return (t.GetString()!, expires);
    }

    /// The offline lease for this launch, or null for online mode. Same surface as an online lease, no network.
    public static LeaseSession? Lease(string? @explicit, bool quiet = false, DateTimeOffset? now = null, string? envValue = null)
    {
        var path = OfflineKeyPath(@explicit, envValue);
        if (path is null) return null;
        var at = now ?? DateTimeOffset.UtcNow;
        var (token, expires) = LoadOfflineKey(path, at);
        var left = (int)Math.Floor((expires - at).TotalDays);
        if (left < WarnDays && !quiet)
            Console.Error.WriteLine(
                $"[clearcote] [offline] the offline key expires in {left} day(s) ({expires.UtcDateTime:yyyy-MM-dd}); " +
                "install the newer key from your renewal before then.");
        return new LeaseSession(() => token, "offline", () => Task.CompletedTask, () => new LaunchToken("", () => { }));
    }

    /// Offline mode never downloads: the binary must come from the offline bundle.
    public static void RequireOfflineBinary(string? offlineKey, string? executablePath, string? envKey = null, string? envBinary = null)
    {
        if (OfflineKeyPath(offlineKey, envKey) is null) return;
        var bin = envBinary ?? Environment.GetEnvironmentVariable("CLEARCOTE_BINARY");
        if (string.IsNullOrEmpty(executablePath) && string.IsNullOrEmpty(bin))
            throw new OfflineKeyError(
                "Offline mode runs the PRO binary from your offline bundle: set ExecutablePath or CLEARCOTE_BINARY.");
    }
}
