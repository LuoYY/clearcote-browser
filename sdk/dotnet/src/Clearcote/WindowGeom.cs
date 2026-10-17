using System.Globalization;
using System.Text;

namespace Clearcote;

/// <summary>
/// The persona browser window: maximized, snapped, or a restored rectangle, derived from a seed and the
/// final screen / work area.
/// </summary>
/// <remarks>
/// <para>
/// A bit-exact port of the engine's <c>components/ungoogled/persona_window_geometry.cc</c> (r39), the
/// same as the Python SDK's <c>_windowgeom.py</c> and the Node SDK's <c>windowgeom.ts</c>. The engine
/// applies it natively to every persona launch (<c>--fingerprint=&lt;seed&gt;</c> on r39+): the headless
/// display becomes the persona's display and the first browser window is created with the persona's
/// bounds and show state, so <c>outerWidth</c>/<c>outerHeight</c>/<c>screenX</c>/<c>screenY</c> are the
/// real window's. The SDK uses this port in its tests (the parity vector is the engine's own output) and
/// to predict a persona's window.
/// </para>
/// <para>
/// Integer arithmetic only: every 64-bit operation in <see cref="ulong"/> (wrapping, like the engine's
/// uint64_t and the Python/Node ports' masking), every other value a small non-negative int, so
/// Python, Node and .NET agree on every rectangle. (Integer division truncates here and floors in
/// Python; the operands are never negative, so the two agree.)
/// </para>
/// </remarks>
internal static class WindowGeom
{
    public const int AlgorithmVersion = 1;
    public const int MinAvailWidth = 800;
    public const int MinAvailHeight = 560;

    private const ulong WindowSalt = 0x5B1E7A3C9D24F681UL;
    private const ulong WindowKeySalt = 0xC2B2AE3D27D4EB4FUL;
    private const int MinWidth = 900;
    private const int MinHeight = 600;
    private const int MinSnapWidth = 900;
    private const int MaxTopOffset = 65;
    private const int MaxNearOffset = 60;

    /// <summary>The engine's platform codes.</summary>
    public static readonly IReadOnlyDictionary<string, int> Platforms =
        new Dictionary<string, int> { ["windows"] = 0, ["linux"] = 1, ["macos"] = 2 };

    // (maximized, near, snap, restored) per-mille by work-area width: <= 1440, <= 1680, <= 1920,
    // <= 2560, wider. Each row sums to 1000. Same table as the engine.
    private static readonly (int Maximized, int Near, int Snap, int Restored)[] Weights =
    {
        (700, 50, 0, 250),
        (670, 50, 0, 280),
        (640, 50, 10, 300),
        (530, 50, 10, 410),
        (450, 50, 10, 490),
    };

    /// <summary>A derived window.</summary>
    /// <param name="Valid">False when the work area is too small (or inconsistent) to be a desktop's.</param>
    /// <param name="State">"maximized" or "normal"; null when not valid.</param>
    /// <param name="Kind">"maximized", "near", "snap-left", "snap-right" or "restored"; null when not valid.</param>
    /// <param name="X">The normal (restored) rectangle, relative to the work area's top-left; a maximized window restores here.</param>
    /// <param name="Y">See <paramref name="X"/>.</param>
    /// <param name="Width">See <paramref name="X"/>.</param>
    /// <param name="Height">See <paramref name="X"/>.</param>
    public sealed record PersonaWindow(bool Valid, string? State, string? Kind, int X, int Y, int Width, int Height);

    private static ulong Mix(ulong z)
    {
        unchecked
        {
            z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
            z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
            return z ^ (z >> 31);
        }
    }

    /// <summary>SplitMix64, as in the engine.</summary>
    private sealed class SplitMix
    {
        private ulong _state;

        public SplitMix(ulong state) => _state = state;

        public ulong Next()
        {
            unchecked { _state += 0x9E3779B97F4A7C15UL; }
            return Mix(_state);
        }

        public int Below(int n) => (int)(Next() % (ulong)n);

        public int Range(int lo, int hi) => lo + Below(hi - lo + 1);
    }

    private static ulong U32(int v) => (uint)v;

    private static ulong InputHash(ulong seed, int sw, int sh, int aw, int ah, int platform, ulong salt)
    {
        var h = Mix(seed ^ salt);
        h = Mix(h ^ ((U32(sw) << 32) | U32(sh)));
        h = Mix(h ^ ((U32(aw) << 32) | U32(ah)));
        h = Mix(h ^ (((ulong)(uint)platform << 8) | (uint)AlgorithmVersion));
        return h;
    }

    private static int Permille(int total, int pm) => (int)((long)total * pm / 1000);

    private static int Skewed(SplitMix s)
    {
        var a = s.Range(0, 1000);
        var b = s.Range(0, 1000);
        return Math.Min(a, b);
    }

    private static int Span(SplitMix s, int total, int loPm, int hiPm, int floor)
    {
        var lo = Math.Min(total, Math.Max(floor, Permille(total, loPm)));
        var hi = Math.Min(total, Math.Max(lo, Permille(total, hiPm)));
        return s.Range(lo, hi);
    }

    private static int Clamp(int v, int lo, int hi) => v < lo ? lo : v > hi ? hi : v;

    private static (int X, int Y, int Width, int Height) DrawRestored(SplitMix s, int aw, int ah)
    {
        var minW = Math.Min(aw, MinWidth);
        var minH = Math.Min(ah, MinHeight);
        var w = Span(s, aw, 420, 900, minW);
        var h = s.Below(10) < 8 ? Span(s, ah, 780, 1000, minH) : Span(s, ah, 450, 780, minH);
        var loW = Math.Max(minW, (11 * h + 9) / 10);
        var hiW = Math.Min(aw, 2 * h);
        if (loW <= hiW) w = Clamp(w, loW, hiW);
        else h = Clamp(h, Math.Max(minH, (w + 1) / 2), Math.Min(ah, Math.Max(minH, 10 * w / 11)));
        var (slackX, slackY) = (aw - w, ah - h);
        var y = Permille(Math.Min(slackY, MaxTopOffset), Skewed(s));
        var where = s.Below(100);
        int x;
        if (where < 25) x = 0;
        else if (where < 35) x = Permille(slackX, s.Range(1, 100));
        else
        {
            var a = s.Range(0, 1000);
            var b = s.Range(0, 1000);
            x = Permille(slackX, (a + b) / 2);
        }
        return (x, y, w, h);
    }

    /// <summary>The engine's code for a platform name ("windows" = 0, "linux" = 1, "macos" = 2; anything else 0).</summary>
    public static int PlatformCode(string? platform) =>
        Platforms.TryGetValue((platform ?? "windows").ToLowerInvariant(), out var code) ? code : 0;

    /// <summary>The window for an identity seed on a work area (see <see cref="PersonaWindow"/>).</summary>
    public static PersonaWindow DeriveWindow(ulong seed, int screenWidth, int screenHeight, int availWidth, int availHeight,
        string? platform = "windows") =>
        DeriveWindow(seed, screenWidth, screenHeight, availWidth, availHeight, PlatformCode(platform));

    /// <summary><see cref="DeriveWindow(ulong, int, int, int, int, string?)"/> with the engine's platform code.</summary>
    public static PersonaWindow DeriveWindow(ulong seed, int screenWidth, int screenHeight, int availWidth, int availHeight,
        int platform)
    {
        int sw = screenWidth, sh = screenHeight, aw = availWidth, ah = availHeight;
        if (aw < MinAvailWidth || ah < MinAvailHeight || sw < aw || sh < ah)
            return new PersonaWindow(false, null, null, 0, 0, 0, 0);
        var s = new SplitMix(InputHash(seed, sw, sh, aw, ah, platform, WindowSalt));
        var (mx, near, snapW, _) = Weights[aw <= 1440 ? 0 : aw <= 1680 ? 1 : aw <= 1920 ? 2 : aw <= 2560 ? 3 : 4];
        var snap = platform == Platforms["windows"] && aw / 2 >= MinSnapWidth ? snapW : 0;
        var pick = s.Below(1000);
        pick -= mx;
        if (pick < 0)
        {
            var r = DrawRestored(s, aw, ah);
            return new PersonaWindow(true, "maximized", "maximized", r.X, r.Y, r.Width, r.Height);
        }
        pick -= near;
        if (pick < 0)
        {
            var w = Span(s, aw, 900, 990, Math.Min(aw, MinWidth));
            var h = Span(s, ah, 900, 990, Math.Min(ah, MinHeight));
            var x = Permille(Math.Min(aw - w, MaxNearOffset), s.Range(0, 1000));
            var y = Permille(Math.Min(ah - h, MaxNearOffset), s.Range(0, 1000));
            return new PersonaWindow(true, "normal", "near", x, y, w, h);
        }
        pick -= snap;
        if (pick < 0)
        {
            var right = s.Below(5) < 2;
            var half = aw / 2;
            return new PersonaWindow(true, "normal", right ? "snap-right" : "snap-left", right ? aw - half : 0, 0, half, ah);
        }
        var restored = DrawRestored(s, aw, ah);
        return new PersonaWindow(true, "normal", "restored", restored.X, restored.Y, restored.Width, restored.Height);
    }

    /// <summary>The engine's saved-placement tag value (PersonaWindowKey) for these inputs.</summary>
    public static ulong WindowKey(ulong seed, int screenWidth, int screenHeight, int availWidth, int availHeight,
        string? platform = "windows") =>
        WindowKey(seed, screenWidth, screenHeight, availWidth, availHeight, PlatformCode(platform));

    /// <summary><see cref="WindowKey(ulong, int, int, int, int, string?)"/> with the engine's platform code.</summary>
    public static ulong WindowKey(ulong seed, int screenWidth, int screenHeight, int availWidth, int availHeight, int platform) =>
        InputHash(seed, screenWidth, screenHeight, availWidth, availHeight, platform, WindowKeySalt);

    /// <summary>FNV-1a 64 of a UTF-8 string: the engine's PersonaSeedFromString for non-numeric values.</summary>
    public static ulong Fnv1a64(string text)
    {
        var h = 0xCBF29CE484222325UL;
        foreach (var b in Encoding.UTF8.GetBytes(text))
        {
            h ^= b;
            unchecked { h *= 0x100000001B3UL; }
        }
        return h;
    }

    /// <summary>
    /// The engine's persona_seed for a <c>--fingerprint</c> value: the value itself when it is an unsigned
    /// 64-bit decimal (ASCII digits only), else FNV-1a 64 of it, then one SplitMix64 step (MixedPersonaSeed).
    /// </summary>
    public static ulong MixedSeed(string rawSeed)
    {
        var s = rawSeed ?? "";
        var seed = s.Length > 0 && s.All(c => c is >= '0' and <= '9')
                   && ulong.TryParse(s, NumberStyles.None, CultureInfo.InvariantCulture, out var n)
            ? n
            : Fnv1a64(s);
        unchecked { return Mix(seed + 0x9E3779B97F4A7C15UL); }
    }
}
