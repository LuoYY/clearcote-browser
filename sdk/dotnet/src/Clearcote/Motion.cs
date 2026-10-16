using System;
using System.Collections.Generic;

namespace Clearcote;

// Motor-persona model — a faithful port of sdk/node/src/motion.ts and sdk/python/clearcote/_motion.py.
//
// WHY IT IS A PORT AND NOT A REWRITE. The persona is derived from the FINGERPRINT SEED, so the same
// identity must produce the same motor signature in every SDK: a profile driven from Python one day
// and from .NET the next has to move the same way, or the "stable per-identity persona" the other two
// SDKs promise is a lie the moment a caller switches language. That means the RNG and the hash have to
// agree bit for bit, not merely "be random" — which is why Mulberry32 and the FNV-1a seed hash below
// are transcribed operation for operation rather than replaced with System.Random and GetHashCode.
//
// Two places where C# and JavaScript disagree and the JS behaviour is the one that must win:
//   * Math.imul is a 32-bit signed multiply that WRAPS. C# `*` on int wraps only in an unchecked
//     context, and the project may compile checked, so Imul() below is explicit.
//   * `>>> 0` coerces to uint32. C# `>>` on int is arithmetic (sign-propagating), so every place the
//     TS reads `>>> n` uses a uint here.
//
// PlanAmbient is deliberately not ported yet — it exists in the other two SDKs for pre-challenge idle
// entropy and nothing in the .NET surface calls it. Porting it without a caller would be untested code.

/// <summary>Deterministic 0..1 generator, matching the other SDKs' mulberry32.</summary>
public delegate double Rng();

/// <summary>The per-identity motor signature. Ranges sit inside measured human envelopes.</summary>
public sealed class Persona
{
    public uint Seed { get; init; }
    /// <summary>Cursor sampling cadence (Hz) → step spacing ≈ 1000/DeviceHz ms (real mice: 125–1000 Hz).</summary>
    public double DeviceHz { get; init; }
    /// <summary>Physiological tremor frequency (Hz), band 8–12.</summary>
    public double TremorHz { get; init; }
    public double TremorAmp { get; init; }
    /// <summary>Ornstein–Uhlenbeck drift amplitude (px) — slow low-frequency wander.</summary>
    public double DriftAmp { get; init; }
    public double DriftTau { get; init; }
    public double Jitter { get; init; }
    /// <summary>Fitts intercept a (ms) and slope b (ms/bit).</summary>
    public double FittsA { get; init; }
    public double FittsB { get; init; }
    public double PrimaryFrac { get; init; }
    public double Overshoot { get; init; }
    public int MaxCorrections { get; init; }
    public double ApproachBias { get; init; }
    /// <summary>The HUMAN BANDS every timing draw is clamped to (ms); the per-identity medians inside them come
    /// from the salted timing stream (<see cref="Motion.TimingTraitsOf"/>). Grab hesitation after mouse down:</summary>
    public double GrabMinMs { get; init; } = 130;
    public double GrabMaxMs { get; init; } = 360;
    public double ReleaseMinMs { get; init; } = 90;
    public double ReleaseMaxMs { get; init; } = 230;
    /// <summary>Click press-hold band (mousedown→mouseup), 55–230 ms; a scripted click is ~2ms.</summary>
    public double ClickHoldMinMs { get; init; } = 55;
    public double ClickHoldMaxMs { get; init; } = 230;
    /// <summary>Per-key dwell band (keydown→keyup), 45–120 ms; scripted typing ~2–3ms.</summary>
    public double KeyDwellMinMs { get; init; } = 45;
    public double KeyDwellMaxMs { get; init; } = 120;
    /// <summary>The persona's own aim stream (<see cref="Motion.ClickPoint"/>), created on first use so the landing
    /// sequence is reproducible per seed. Not part of the identity.</summary>
    internal Rng? AimRng { get; set; }
}

public readonly record struct Point(double X, double Y);
/// <summary>One dispatched sample: move to (X,Y), then sleep SleepMs before the next.</summary>
public readonly record struct Step(double X, double Y, double SleepMs);
public readonly record struct Box(double X, double Y, double Width, double Height);
/// <summary>Per-identity path-shape traits (see Motion.ShapeTraitsOf).</summary>
public readonly record struct ShapeTraits(double OvershootRate, double Curve, double DragTempo);
/// <summary>Per-identity timing medians (ms) and tempo (see Motion.TimingTraitsOf).</summary>
public readonly record struct TimingTraits(double ClickHoldMs, double KeyDwellMs, double GrabMs, double ReleaseMs,
    double DblclickGapMs, double PauseScale, double TypoRate);
/// <summary>Per-identity aiming traits (see Motion.AimTraitsOf).</summary>
public readonly record struct AimTraits(double Spread, double Undershoot, double SideBias);

public static class Motion
{
    // ---- primitives that must match the other SDKs exactly -------------------------------------

    /// <summary>JavaScript Math.imul: 32-bit signed multiply that wraps.</summary>
    private static int Imul(int a, int b) => unchecked((int)((long)a * b));

    /// <summary>mulberry32, transcribed from the TS. Same seed ⇒ same stream in every SDK.</summary>
    public static Rng Mulberry32(uint seedInt)
    {
        uint a = seedInt;
        return () =>
        {
            unchecked
            {
                a = (uint)((int)a + 0x6d2b79f5);
                int t = Imul((int)(a ^ (a >> 15)), (int)(1u | a));
                t = (t + Imul(t ^ (int)((uint)t >> 7), (int)(61u | (uint)t))) ^ t;
                return ((uint)t ^ ((uint)t >> 14)) / 4294967296.0;
            }
        };
    }

    /// <summary>FNV-1a hash of an arbitrary seed to a 32-bit int for the persona RNG.</summary>
    public static uint HashSeed(object? seed)
    {
        if (seed is null) return (uint)(Random.Shared.NextDouble() * 0xffffffff);
        if (seed is int or long or double or float)
        {
            double d = Convert.ToDouble(seed);
            // JS `>>> 0` is ToUint32: the product modulo 2^32 (fmod is exact). A cast to int of a
            // product this size is out of range, and x64 answers 0x80000000 for every nonzero seed.
            if (double.IsFinite(d)) return (uint)(Math.Abs(d) * 2654435761.0 % 4294967296.0);
        }
        string s = seed.ToString() ?? "";
        unchecked
        {
            int h = (int)0x811c9dc5;
            // charCodeAt is a UTF-16 code unit, which is exactly what indexing a C# string yields.
            foreach (char c in s)
            {
                h ^= c;
                h = Imul(h, 0x01000193);
            }
            return (uint)h;
        }
    }

    /// <summary>Standard-normal sample (Box–Muller) from a given RNG.</summary>
    public static double GaussFrom(Rng rng)
    {
        double u = 0, v = 0;
        while (u == 0) u = rng();
        while (v == 0) v = rng();
        return Math.Sqrt(-2 * Math.Log(u)) * Math.Cos(2 * Math.PI * v);
    }

    private static double Lerp(double a, double b, double t) => a + (b - a) * t;
    private static double Clamp(double v, double lo, double hi) => Math.Max(lo, Math.Min(hi, v));
    private static double Log2(double x) => Math.Log(x) / Math.Log(2);
    private static double Hypot(double x, double y) => Math.Sqrt(x * x + y * y);

    // ---- persona -------------------------------------------------------------------------------

    /// <summary>Derive a stable motor persona from a fingerprint seed (or random when unseeded).</summary>
    public static Persona MakePersona(object? seed = null)
    {
        uint seedInt = HashSeed(seed);
        Rng r = Mulberry32(seedInt);
        // Evaluation order matters: each r() consumes the shared stream, so these must be drawn in
        // the same sequence as the TS object literal or the persona diverges across SDKs.
        double deviceHz = Math.Round(Lerp(110, 155, r()));
        double tremorHz = Lerp(8, 12, r());
        double tremorAmp = Lerp(0.12, 0.5, r());
        double driftAmp = Lerp(0.1, 0.4, r());
        double driftTau = Lerp(60, 160, r());
        double jitter = Lerp(0.25, 0.7, r());
        double fittsA = Lerp(90, 150, r());
        double fittsB = Lerp(120, 190, r());
        double primaryFrac = Lerp(0.84, 0.94, r());
        double overshoot = Lerp(0.03, 0.08, r());
        int maxCorrections = r() < 0.15 ? 3 : (r() < 0.6 ? 2 : 1);
        double approachBias = (r() - 0.5) * 0.5;
        return new Persona
        {
            Seed = seedInt,
            DeviceHz = deviceHz,
            TremorHz = tremorHz,
            TremorAmp = tremorAmp,
            DriftAmp = driftAmp,
            DriftTau = driftTau,
            Jitter = jitter,
            FittsA = fittsA,
            FittsB = fittsB,
            PrimaryFrac = primaryFrac,
            Overshoot = overshoot,
            MaxCorrections = maxCorrections,
            ApproachBias = approachBias,
        };
    }

    // Salt for the timing stream. Like the rollover and shape salts, a separate Mulberry32(seed ^ salt) stream keeps
    // MakePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint TimingSalt = 0xC2B2AE35;

    /// <summary>
    /// The persona's timing traits, drawn from a salted stream of its seed: the median click hold (70–140 ms), key
    /// dwell (60–95), drag grab (180–280) and release (120–180), double-click gap (90–140), a tempo on deliberation
    /// pauses (0.8–1.25) and the typo rate (0.5–4 %). Before this every identity shared one fixed window per timing.
    /// Same values as Python <c>timing_traits</c> and Node <c>timingTraits</c>.
    /// </summary>
    public static TimingTraits TimingTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ TimingSalt);
        double clickHold = Lerp(70, 140, r());
        double keyDwell = Lerp(60, 95, r());
        double grab = Lerp(180, 280, r());
        double release = Lerp(120, 180, r());
        double dbl = Lerp(90, 140, r());
        double pause = Lerp(0.8, 1.25, r());
        double typo = Lerp(0.005, 0.04, r());
        return new TimingTraits(clickHold, keyDwell, grab, release, dbl, pause, typo);
    }

    /// <summary><c>median * exp(sigma * N(0,1))</c> clamped to [lo, hi]: a right-skewed human timing.</summary>
    public static double LognormalMs(double median, double sigma, double lo, double hi, Rng rng)
        => Clamp(median * Math.Exp(sigma * GaussFrom(rng)), lo, hi);

    /// <summary>A human mousedown→mouseup hold (ms): log-normal (σ 0.25) around the persona's median, clamped to its band.</summary>
    public static double ClickHold(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return LognormalMs(TimingTraitsOf(p).ClickHoldMs, 0.25, p.ClickHoldMinMs, p.ClickHoldMaxMs, rng);
    }

    /// <summary>A human per-key keydown→keyup dwell (ms): log-normal (σ 0.22) around the persona's median, clamped to its band.</summary>
    public static double KeyDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return LognormalMs(TimingTraitsOf(p).KeyDwellMs, 0.22, p.KeyDwellMinMs, p.KeyDwellMaxMs, rng);
    }

    /// <summary>Grab/release dwell times (ms) for a held-button drag: log-normal (σ 0.2) around the persona's medians.</summary>
    public static (double GrabMs, double ReleaseMs) DragDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        var tt = TimingTraitsOf(p);
        double grab = LognormalMs(tt.GrabMs, 0.2, p.GrabMinMs, p.GrabMaxMs, rng);
        double release = LognormalMs(tt.ReleaseMs, 0.2, p.ReleaseMinMs, p.ReleaseMaxMs, rng);
        return (grab, release);
    }

    /// <summary>The gap between the two presses of a double click (ms): log-normal (σ 0.15) around the persona's
    /// median, clamped to 70–170.</summary>
    public static double DblclickGap(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return LognormalMs(TimingTraitsOf(p).DblclickGapMs, 0.15, 70, 170, rng);
    }

    /// <summary>A deliberation pause (ms) from [lo, hi], scaled by the persona's tempo; without a persona the plain window.</summary>
    public static double PauseMs(Persona? p, double lo, double hi, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double v = Lerp(lo, hi, rng());
        return p is null ? v : v * TimingTraitsOf(p).PauseScale;
    }

    // Salt for the rollover stream. Drawing from Mulberry32(seed ^ salt) instead of adding a field to
    // MakePersona keeps the persona draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint RolloverSalt = 0x9E3779B9;

    /// <summary>
    /// Share of eligible key pairs this persona types with ROLLOVER: the next key goes down before the
    /// previous one comes up, the way people type fast common pairs. Stable per seed, 0.12–0.38.
    /// Measured on r32/r33 before this existed: 0 overlapping keydowns in 30–32 typed keys, because
    /// every character was one press (down, dwell, up) before the next began. Same value as the other
    /// SDKs' rolloverRate / rollover_rate.
    /// </summary>
    public static double RolloverRate(Persona p) => Lerp(0.12, 0.38, Mulberry32(p.Seed ^ RolloverSalt)());

    /// <summary>
    /// Pixels one wheel notch scrolls, by the platform the persona claims. A wheel event sent over CDP
    /// always carries wheelDelta ±120 (input_handler.cc sets wheel_ticks to ±1 whatever the delta), and
    /// a real notch is the only event whose distance goes with a 120 wheelDelta:
    ///   windows 100 = 3 lines × 100/3 px (web_input_event_builders_win.cc, default 3 lines per notch)
    ///   linux   120 = ui::MouseWheelEvent::kWheelDelta (event.cc); measured 120/-120 per X11 notch
    ///   macos    40 = kScrollbarPixelsPerCocoaTick (cocoa_event_utils.h)
    /// android is not measured; it gets the windows value.
    /// </summary>
    public static readonly IReadOnlyDictionary<string, int> WheelNotchPx = new Dictionary<string, int>
    {
        ["windows"] = 100, ["linux"] = 120, ["macos"] = 40, ["android"] = 100,
    };

    /// <summary>
    /// Whole notches for a requested scroll distance: the nearest count, at least one when the caller
    /// asked for any scroll at all. A wheel cannot move 39px; it moves notches. Rounds half up like JS
    /// Math.round — Math.Round's default is banker's rounding, which would turn 250/100 into 2, not 3.
    /// </summary>
    public static int WheelNotches(double delta, double notchPx)
    {
        if (delta == 0 || double.IsNaN(delta)) return 0;   // NaN: JS plans no events for it either
        return Math.Max(1, (int)Math.Floor(Math.Abs(delta) / notchPx + 0.5));
    }

    /// <summary>Map navigator.userAgentData.platform / navigator.platform to a <see cref="WheelNotchPx"/> key.</summary>
    public static string? PlatformFromNavigator(string? value)
    {
        string v = (value ?? "").ToLowerInvariant();
        if (v.Length == 0) return null;
        if (v.Contains("android")) return "android";   // before linux: Android's navigator.platform is "Linux armv8l"
        if (v.Contains("win")) return "windows";
        if (v.Contains("mac")) return "macos";
        if (v.Contains("linux") || v.Contains("x11") || v.Contains("cros")) return "linux";
        return null;
    }

    // Salt for the aim stream (ClickPoint): the persona's landing-point sequence and its dispersion.
    private const uint AimSalt = 0x27D4EB2F;

    /// <summary>The persona's aiming traits from a salted stream of its seed: dispersion (sd 0.10–0.20 of the box),
    /// undershoot toward the approach side (0.06–0.16) and handedness (ApproachBias × 0.4). Same values as Python
    /// <c>aim_traits</c> and Node <c>aimTraits</c>.</summary>
    public static AimTraits AimTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ AimSalt);
        double spread = Lerp(0.10, 0.20, r());
        double under = Lerp(0.06, 0.16, r());
        return new AimTraits(spread, under, p.ApproachBias * 0.4);
    }

    /// <summary>
    /// A human click point inside <paramref name="box"/>: a 2D gaussian toward the centre with the persona's
    /// dispersion, nudged toward the approach side (undershoot) and sideways by its handedness, clamped inside the
    /// edges. Drawn from the persona's own aim stream unless <paramref name="rng"/> is given, so the landing sequence
    /// is reproducible per seed (it was Random.Shared: unseeded, one dispersion for everyone). Without a persona:
    /// sd 0.15, 12 % undershoot, no handedness.
    /// </summary>
    public static Point ClickPoint(Box box, Point from, Persona? p, Rng? rng = null)
    {
        double spread = 0.15, under = 0.12, side = 0;
        if (p is not null)
        {
            var at = AimTraitsOf(p);
            spread = at.Spread; under = at.Undershoot; side = at.SideBias;
            rng ??= p.AimRng ??= Mulberry32(p.Seed ^ AimSalt ^ 1u);
        }
        rng ??= Random.Shared.NextDouble;
        double cx = box.X + box.Width / 2, cy = box.Y + box.Height / 2;
        double dx = cx - from.X, dy = cy - from.Y;
        double d = Hypot(dx, dy);
        if (d == 0) d = 1;
        double ux = -dx / d, uy = -dy / d;
        double x = cx + ux * box.Width * under + side * box.Width + GaussFrom(rng) * box.Width * spread;
        double y = cy + uy * box.Height * under + GaussFrom(rng) * box.Height * spread;
        // Below the 4px inset the clamp bounds invert and would return a point OUTSIDE the element.
        return new Point(
            box.Width >= 4 ? Clamp(x, box.X + 2, box.X + box.Width - 2) : cx,
            box.Height >= 4 ? Clamp(y, box.Y + 2, box.Y + box.Height - 2) : cy);
    }

    // ---- path planning -------------------------------------------------------------------------

    /// <summary>Minimum-jerk easing s(τ)=10τ³−15τ⁴+6τ⁵.</summary>
    public static double MinJerk(double tau)
    {
        double t = Clamp(tau, 0, 1);
        return t * t * t * (10 + t * (-15 + 6 * t));
    }

    private static double Bez(double p0, double c1, double c2, double p1, double e)
    {
        double m = 1 - e;
        return m * m * m * p0 + 3 * m * m * e * c1 + 3 * m * e * e * c2 + e * e * e * p1;
    }

    private sealed class Vec { public double X, Y; }

    // Salt for the path-shape stream. Like the rollover salt, a separate Mulberry32(seed ^ salt) stream
    // keeps MakePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint ShapeSalt = 0x85ebca6b;

    /// <summary>
    /// The persona's path-shape traits, drawn from a salted stream of its seed: the share of aimed moves
    /// that pass the target and come back (0.15–0.38), a scale on the primary submovement's bow
    /// (0.75–1.3) and a scale on held-drag duration (0.85–1.2). Measured on r38 / SDK 0.48.0 (SC 209):
    /// 1 of 36 planned approaches passed the target, against 15–40 % for real reaches. Same values as
    /// Python <c>shape_traits</c> and Node <c>shapeTraits</c>.
    /// </summary>
    public static ShapeTraits ShapeTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ ShapeSalt);
        double overshootRate = Lerp(0.15, 0.38, r());
        double curve = Lerp(0.75, 1.3, r());
        double dragTempo = Lerp(0.85, 1.2, r());
        return new ShapeTraits(overshootRate, curve, dragTempo);
    }

    // Sub-pixel noise scale. The coloured noise (OU drift + 8–12 Hz tremor + AR(1) jitter) stays, but
    // smaller and carried mostly while the hand is moving: a mouse resting on the pad reports no counts,
    // so a slow tail full of ±0.5 px wobble is what a quantizing page turns into 1 px zigzags.
    private const double Noise = 0.6;

    /// <summary>floor(x + 0.5): JavaScript Math.round / Python _js_round, not banker's rounding.</summary>
    private static double JsRound(double x) => Math.Floor(x + 0.5);

    /// <summary>Time warp 1−(1−τ)^skew: skew &gt; 1 moves the min-jerk speed peak before the midpoint.</summary>
    private static double Skewed(double tau, double skew)
    {
        double t = Clamp(tau, 0, 1);
        return skew != 1.0 ? 1 - Math.Pow(1 - t, skew) : t;
    }

    private static double SampleSubmove(
        List<Step> outSteps, Point a, Point b, double durMs, Persona p, Rng rng,
        double t0, Vec drift, Vec jit, double tremorPhase, bool land, double bowFrac, double skew)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y;
        double dist = Hypot(dx, dy);
        if (dist == 0) dist = 1e-6;
        double stepMs = 1000 / p.DeviceHz;
        int n = (int)Math.Min(180, Math.Max(4, JsRound(durMs / stepMs)));
        double nx = -dy / dist, ny = dx / dist;
        // A cubic with both control points offset by c peaks at 0.75 c; the first control point carries
        // more of it, so the widest point of the arc comes before the middle, as in a reach.
        double c = bowFrac * dist / 0.75;
        double c1x = a.X + dx * 0.3 + nx * c * 1.15, c1y = a.Y + dy * 0.3 + ny * c * 1.15;
        double c2x = a.X + dx * 0.7 + nx * c * 0.85, c2y = a.Y + dy * 0.7 + ny * c * 0.85;
        double t = t0;
        for (int i = 1; i <= n; i++)
        {
            double tau = (double)i / n;
            double e = MinJerk(Skewed(tau, skew));
            double px = Bez(a.X, c1x, c2x, b.X, e);
            double py = Bez(a.Y, c1y, c2y, b.Y, e);
            bool last = i == n;
            if (!(last && land))
            {
                // noise follows the speed envelope (min-jerk speed 30τ²(1−τ)² peaks at 1.875)
                double v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
                double g = Noise * (0.35 + 0.65 * v);
                double k = stepMs / p.DriftTau;
                drift.X += -k * drift.X + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                drift.Y += -k * drift.Y + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                double tAmp = p.TremorAmp * (0.7 + 0.3 * Math.Sin(2 * Math.PI * t / 850 + tremorPhase));
                double trX = tAmp * Math.Sin(tremorPhase + 2 * Math.PI * p.TremorHz * t / 1000);
                double trY = tAmp * Math.Sin(tremorPhase + 1.0 + 2 * Math.PI * (p.TremorHz * 0.93) * t / 1000);
                // AR(1) coloured jitter, not white noise: white noise adds a fresh speed reversal every
                // sample and manufactures spurious submovements.
                jit.X = 0.7 * jit.X + GaussFrom(rng) * p.Jitter * 0.22;
                jit.Y = 0.7 * jit.Y + GaussFrom(rng) * p.Jitter * 0.22;
                px += (drift.X + trX + jit.X) * g;
                py += (drift.Y + trY + jit.Y) * g;
            }
            else { px = b.X; py = b.Y; }
            double sleepMs = stepMs * Lerp(0.88, 1.12, rng());
            outSteps.Add(new Step(px, py, sleepMs));
            t += sleepMs;
        }
        return t;
    }

    /// <summary>
    /// Plan a cursor move from→to as minimum-jerk submovements (primary + corrections) with a
    /// Fitts-scaled duration and coloured noise. Returns the samples to dispatch. A move with a button
    /// held (<paramref name="held"/>) or one that seats a handle (<paramref name="settle"/>) is a drag,
    /// planned by <see cref="PlanDrag"/>.
    /// <para>Shape (SC 209/210): the primary submovement arcs (peak sideways deviation ~6–18 % of the
    /// distance, per-persona curvature and handedness), reaches its speed peak before the middle of the
    /// move, and either stops short of the target (a short correction closes the gap) or, in
    /// <see cref="ShapeTraits.OvershootRate"/> of moves, passes it by 3–28 px and comes back.
    /// Straightness of the planned points lands around 0.85–0.95, the band of real reaches.</para>
    /// </summary>
    public static List<Step> PlanMove(Point from, Point to, Persona p,
        double targetW = 24, bool settle = false, Rng? rng = null, bool held = false)
    {
        if (settle || held) return PlanDrag(from, to, p, settle, rng);
        rng ??= Random.Shared.NextDouble;
        double W = Math.Max(6, targetW);
        double D = Hypot(to.X - from.X, to.Y - from.Y);
        var outSteps = new List<Step>();
        if (D < 1.5) { outSteps.Add(new Step(to.X, to.Y, 1000 / p.DeviceHz)); return outSteps; }

        double id = Log2(D / W + 1);
        double mtLo = Math.Min(1700, Math.Max(70, 40 + D * 0.6));
        double mt = Clamp((p.FittsA + p.FittsB * id) * (0.85 + 0.3 * rng()), mtLo, 1700);
        var tr = ShapeTraitsOf(p);
        double ux = (to.X - from.X) / D, uy = (to.Y - from.Y) / D;

        var targets = new List<Point>();
        bool over = false;
        if (D >= 40)
        {
            over = rng() < tr.OvershootRate;
            double along = over
                ? D + Clamp(D * Lerp(0.03, 0.08, rng()), 3, 28)
                : D * Clamp(p.PrimaryFrac + 0.05 + GaussFrom(rng) * 0.015, 0.88, 0.985);
            double lat = GaussFrom(rng) * Math.Min(6, D * 0.02);
            targets.Add(new Point(from.X + ux * along - uy * lat, from.Y + uy * along + ux * lat));
            if (rng() < 0.08 * p.MaxCorrections)
            {
                Point cur = targets[0];
                double close = Lerp(0.55, 0.8, rng());
                double mx = cur.X + (to.X - cur.X) * close + GaussFrom(rng) * 1.0;
                double my = cur.Y + (to.Y - cur.Y) * close + GaussFrom(rng) * 1.0;
                targets.Add(new Point(mx, my));
            }
        }
        targets.Add(to);

        var segDur = new List<double>();
        if (targets.Count == 1) segDur.Add(mt);
        else
        {
            segDur.Add(mt * 0.78);
            double rest = mt * 0.22;
            int nc = targets.Count - 1;
            for (int i = 0; i < nc; i++) segDur.Add(rest / nc * Lerp(0.8, 1.2, rng()));
        }

        // The primary's arc: handedness sign from the persona (1 move in 5 bends the other way), size
        // from the persona's curvature trait. Corrections are short and nearly straight.
        double sign = p.ApproachBias >= 0 ? 1 : -1;
        if (rng() < 0.2) sign = -sign;
        double bow = sign * Math.Min(0.25, (0.08 + Math.Abs(GaussFrom(rng)) * 0.11) * tr.Curve * (0.75 + Math.Abs(p.ApproachBias)));
        if (D * Math.Abs(bow) > 110) bow = bow < 0 ? -110 / D : 110 / D;

        var drift = new Vec();
        var jit = new Vec();
        double tremorPhase = rng() * 2 * Math.PI;
        Point aPt = from;
        double t = 0;
        for (int s = 0; s < targets.Count; s++)
        {
            Point b = targets[s];
            bool last = s == targets.Count - 1;
            bool first = s == 0;
            t = SampleSubmove(outSteps, aPt, b, Math.Max(20, segDur[s]), p, rng, t, drift, jit, tremorPhase, last,
                first ? bow : bow * 0.15, first ? 1.25 : 1.0);
            if (!last && outSteps.Count > 0)
            {
                // motor re-planning gap; a reversal after passing the target comes back quicker
                var lastStep = outSteps[^1];
                double gap = over && first ? Lerp(25, 90, rng()) : Lerp(40, 120, rng());
                outSteps[^1] = lastStep with { SleepMs = lastStep.SleepMs + gap };
            }
            aPt = b;
        }
        return outSteps;
    }

    /// <summary>
    /// Plan a held-button drag from→to (slider handle, drag-and-drop, range thumb). Measured on r38 /
    /// SDK 0.48.0 (SC 209): the held leg reused the aimed-move planner, whose arc put the hand up to
    /// 47 px off the line between grab and drop — off a 40 px slider track. A drag is a slower, flatter
    /// movement: one skewed min-jerk leg along the line with at most ~5 px of arc and ±6 px of total
    /// sideways error, 0.5–2 s for 300–400 px, and now and then (about 0.6× the persona's overshoot rate)
    /// a 2.5–10 px overshoot and pull-back. <paramref name="settle"/> adds a couple of sub-pixel seating
    /// jiggles before landing exactly on <paramref name="to"/>.
    /// </summary>
    public static List<Step> PlanDrag(Point from, Point to, Persona p, bool settle = false, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double D = Hypot(to.X - from.X, to.Y - from.Y);
        var outSteps = new List<Step>();
        double stepMs = 1000 / p.DeviceHz;
        if (D < 1.5) { outSteps.Add(new Step(to.X, to.Y, stepMs)); return outSteps; }
        var tr = ShapeTraitsOf(p);
        double ux = (to.X - from.X) / D, uy = (to.Y - from.Y) / D;
        double mt = Clamp((260 + 1.7 * D) * tr.DragTempo * Lerp(0.9, 1.1, rng()), Math.Min(500, 150 + 1.5 * D), 2000);
        bool over = D >= 60 && rng() < tr.OvershootRate * 0.6;
        double o = over ? Clamp(D * Lerp(0.01, 0.03, rng()), 2.5, 10) : 0;
        double sign = p.ApproachBias >= 0 ? 1 : -1;
        double h = sign * Math.Min(5, D * 0.015) * Lerp(0.3, 1.0, rng());
        double tremorPhase = rng() * 2 * Math.PI;

        var legs = new List<(double S0, double S1, double Dur, double H)> { (0, D + o, mt * (over ? 0.86 : 1.0), h) };
        if (over) legs.Add((D + o, D, mt * 0.14, 0));
        double drift = 0, jit = 0, t = 0;
        for (int li = 0; li < legs.Count; li++)
        {
            var (s0, s1, dur, hh) = legs[li];
            int n = (int)Math.Min(360, Math.Max(4, JsRound(dur / stepMs)));
            double dt = dur / n;
            bool lastLeg = li == legs.Count - 1;
            for (int i = 1; i <= n; i++)
            {
                double tau = (double)i / n;
                double e = MinJerk(Skewed(tau, 1.2));
                double along = s0 + (s1 - s0) * e;
                double v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
                double k = dt / p.DriftTau;
                drift += -k * drift + Math.Sqrt(2 * k) * p.DriftAmp * 0.8 * GaussFrom(rng);
                jit = 0.7 * jit + GaussFrom(rng) * p.Jitter * 0.12;
                double tAmp = p.TremorAmp * (0.7 + 0.3 * Math.Sin(2 * Math.PI * t / 850 + tremorPhase));
                double tremor = tAmp * Math.Sin(tremorPhase + 2 * Math.PI * p.TremorHz * t / 1000);
                along += tAmp * 0.6 * Math.Sin(tremorPhase + 1.0 + 2 * Math.PI * (p.TremorHz * 0.93) * t / 1000) * v;
                double side = hh * Math.Sin(Math.PI * e) + (drift + tremor + jit) * (0.6 + 0.4 * v);
                side = Clamp(side, -6, 6);
                double px, py;
                if (lastLeg && i == n && !settle) { px = to.X; py = to.Y; }
                else { px = from.X + ux * along - uy * side; py = from.Y + uy * along + ux * side; }
                double sl = dt * Lerp(0.88, 1.12, rng());
                outSteps.Add(new Step(px, py, sl));
                t += sl;
            }
            if (!lastLeg && outSteps.Count > 0)
            {
                var ls = outSteps[^1];
                outSteps[^1] = ls with { SleepMs = ls.SleepMs + Lerp(30, 80, rng()) };   // notices, stops, pulls back
            }
        }

        if (settle)
        {
            // A couple of tiny sub-pixel seating jiggles, then land exactly (handle "clunk" into place).
            int jig = 1 + (int)Math.Floor(rng() * 2);
            for (int i = 0; i < jig; i++)
            {
                double jx = to.X + Clamp(GaussFrom(rng) * 0.8, -1.5, 1.5);
                double jy = to.Y + Clamp(GaussFrom(rng) * 0.8, -1.5, 1.5);
                outSteps.Add(new Step(jx, jy, Lerp(30, 90, rng())));
            }
            outSteps.Add(new Step(to.X, to.Y, stepMs));
        }
        return outSteps;
    }
}
