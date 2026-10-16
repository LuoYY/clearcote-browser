using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;

namespace Clearcote;

/// <summary>One wheel event of a planned scroll: the deltas and the wait AFTER it (ms).</summary>
public readonly record struct WheelEvent(double Dx, double Dy, double SleepMs);

/// <summary>One trackpad swipe: the finger phase, the inertia tail and the gap before the next swipe.</summary>
public sealed record TrackpadSwipe(List<WheelEvent> Finger, List<WheelEvent> Tail, double GapMs);

/// <summary>The nearest scrollable ancestor of a target: its visible client rect and the axes it scrolls.</summary>
public sealed record Scroller(double X, double Y, double Width, double Height, bool AxisX, bool AxisY);

/// <summary>What <see cref="Scroll.BringIntoViewAsync"/> needs from the humanizer.</summary>
internal interface IScrollIO
{
    Rng Rng { get; }
    Task<(double W, double H)> ViewportAsync();
    (double X, double Y) Pointer { get; }
    bool Placed { get; }
    bool Held { get; }
    Task GlideAsync(double x, double y, double targetW);
    /// <summary>ONE native wheel event.</summary>
    Task WheelAsync(double dx, double dy);
    Task SleepAsync(double ms);
    Task<int> NotchPxAsync();
    Task<string> DeviceAsync();
}

/// <summary>
/// Wheel-driven scrolling for the humanizer: scroll-into-view, the shared wheel anchor, trackpad streams.
/// Port of Python <c>clearcote/_scroll.py</c> and Node <c>scroll.ts</c> (same numbers; pinned by
/// sdk/testdata/humanize_scroll_vectors.json).
///
/// Measured on r42 / SDK 0.51.0 (SC 232 review): before a humanized click, fill or hold on an off-screen
/// target the SDK scrolled PROGRAMMATICALLY (ScrollIntoViewIfNeededAsync, an instant scrollBy), so the page
/// saw a <c>scroll</c> event with no wheel before it and the target at the exact centre. A hand brings a
/// target in with the wheel: whole notches (a mouse) or a decaying pixel stream (a trackpad), undershooting
/// then correcting, and leaves it somewhere in the upper half of the viewport.
///
/// Playwright's <c>Mouse.WheelAsync</c> sends pixel deltas, so a page sees <c>deltaMode 0</c> whatever the
/// persona; a notch persona's deltas are whole multiples of its notch (100 px Windows, 120 Linux, 40 macOS),
/// a trackpad persona's are 2-40 px events at 60-120/s ending in an inertia tail.
/// </summary>
public static class Scroll
{
    private static double Lerp(double a, double b, double t) => a + (b - a) * t;
    private static double Clamp(double v, double lo, double hi) => Math.Max(lo, Math.Min(hi, v));
    private static double JsRound(double x) => Math.Floor(x + 0.5);
    private static double Hypot(double x, double y) => Math.Sqrt(x * x + y * y);

    // ---- the anchor (one set of numbers in all three SDKs; .NET used 0.5/0.38, sd 0.12/0.10, y clamp 0.12-0.75)
    public static readonly (double C, double Sd, double Lo, double Hi) AnchorX = (0.5, 0.10, 0.12, 0.88);
    public static readonly (double C, double Sd, double Lo, double Hi) AnchorY = (0.34, 0.09, 0.10, 0.62);
    public const double AnchorTargetW = 140;
    public const double AnchorMarginPx = 8;
    /// <summary>A resting hand's drift between wheel events: ONE native move, N(0, 1.5) x N(0, 1.2) px
    /// (.NET used to draw 2.2 x 2.2).</summary>
    public static readonly (double X, double Y) DriftSd = (1.5, 1.2);

    // ---- scroll-into-view
    public static readonly (double Lo, double Hi) TargetFracRange = (0.25, 0.60);
    public static readonly (double Lo, double Hi) UndershootRange = (0.60, 0.90);
    public const int NotchBudget = 60;
    public const double SettleMs = 50, SettleCapMs = 1200, MinMovePx = 0.5;
    private static readonly (double Lo, double Hi) NotchGapMs = (18, 55), FlickGapMs = (110, 320), ReadPauseMs = (150, 450);
    private static readonly (double Lo, double Hi) FlickNotches = (2, 5);
    private const double ReadPauseRate = 0.07;

    // ---- trackpad
    public static readonly (double Lo, double Hi) TrackpadHz = (60, 120), TrackpadPx = (2, 40);
    public const double InertiaMinMs = 300, InertiaStopPx = 1, TrackpadPauseRate = 0.2;
    public static readonly (double Lo, double Hi) TrackpadPauseMs = (120, 400), TrackpadGapMs = (80, 200);
    public const double TrackpadStrokePx = 900, TrackpadPeakPx = 38, TrackpadNudgePx = 60;

    /// <summary>The wheel anchor for a vw x vh viewport (two gaussians from <paramref name="rng"/>, x first).</summary>
    public static (double X, double Y) AnchorPoint(double vw, double vh, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double x = Clamp(vw * AnchorX.C + Motion.GaussFrom(rng) * vw * AnchorX.Sd, vw * AnchorX.Lo, vw * AnchorX.Hi);
        double y = Clamp(vh * AnchorY.C + Motion.GaussFrom(rng) * vh * AnchorY.Sd, vh * AnchorY.Lo, vh * AnchorY.Hi);
        return (x, y);
    }

    /// <summary>Should the pointer be re-homed before a wheel? Never with a button down; not when placed and inside.</summary>
    public static bool AnchorNeeded(double x, double y, double vw, double vh, bool placed, bool held)
    {
        if (held) return false;
        double m = AnchorMarginPx;
        return !(placed && m <= x && x <= vw - m && m <= y && y <= vh - m);
    }

    /// <summary>(dx, dy) of the hand's drift between wheel events, x first.</summary>
    public static (double Dx, double Dy) WheelDrift(Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return (Motion.GaussFrom(rng) * DriftSd.X, Motion.GaussFrom(rng) * DriftSd.Y);
    }

    /// <summary>"trackpad" or "mouse": the caller's option when it names one, else a trackpad for a macOS persona.</summary>
    public static string InputDevice(string? platform, string? option = null)
    {
        string o = (option ?? "").Trim().ToLowerInvariant();
        if (o == "trackpad" || o == "touchpad") return "trackpad";
        if (o == "mouse") return "mouse";
        return (platform ?? "").ToLowerInvariant() == "macos" ? "trackpad" : "mouse";
    }

    public static double TargetFrac(Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return Lerp(TargetFracRange.Lo, TargetFracRange.Hi, rng());
    }

    public static bool InView(Box b, Box view)
        => b.X >= view.X && b.Y >= view.Y && b.X + b.Width <= view.X + view.Width && b.Y + b.Height <= view.Y + view.Height;

    /// <summary>(dx, dy) the content must move so <paramref name="b"/> shows inside <paramref name="view"/>.</summary>
    public static (double Dx, double Dy) ScrollNeed(Box b, Box view, double frac)
    {
        double dx = 0, dy = 0;
        if (b.Y < view.Y || b.Y + b.Height > view.Y + view.Height)
            dy = b.Height > view.Height * 0.9 ? b.Y - (view.Y + view.Height * 0.05) : (b.Y + b.Height / 2) - (view.Y + view.Height * frac);
        if (b.X < view.X || b.X + b.Width > view.X + view.Width)
            dx = b.Width > view.Width * 0.9 ? b.X - (view.X + view.Width * 0.05) : (b.X + b.Width / 2) - (view.X + view.Width / 2);
        return (dx, dy);
    }

    /// <summary>(dx, dy) still to scroll to where ScrollNeed aimed, on the axes that were out at the start.</summary>
    public static (double Dx, double Dy) AimNeed(Box b, Box view, double frac, bool ax, bool ay)
    {
        double dx = 0, dy = 0;
        if (ay) dy = b.Height > view.Height * 0.9 ? b.Y - (view.Y + view.Height * 0.05) : (b.Y + b.Height / 2) - (view.Y + view.Height * frac);
        if (ax) dx = b.Width > view.Width * 0.9 ? b.X - (view.X + view.Width * 0.05) : (b.X + b.Width / 2) - (view.X + view.Width / 2);
        return (dx, dy);
    }

    public static double Undershoot(double needPx, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return needPx * Lerp(UndershootRange.Lo, UndershootRange.Hi, rng());
    }

    private static (List<double> Finger, List<double> Tail) Swipe(double dist, double dt, double tauR, double decayMs)
    {
        (List<double>, List<double>) Build(int nfull, double amp)
        {
            int k = Math.Max(2, (int)Math.Floor(nfull * tauR));
            var finger = new List<double>();
            double prev = 0;
            for (int i = 1; i <= k; i++) { double e = Motion.MinJerk((double)i / nfull); finger.Add(amp * (e - prev)); prev = e; }
            double vr = Math.Max(finger[^1], InertiaStopPx * 1.5);
            double tau = dist < TrackpadNudgePx ? decayMs : Math.Max(decayMs, InertiaMinMs * 1.15 / Math.Log(vr / InertiaStopPx));
            var tail = new List<double>();
            double t = dt;
            while (tail.Count < 400)
            {
                double v = vr * Math.Exp(-t / tau);
                if (v < InertiaStopPx) break;
                tail.Add(v);
                t += dt;
            }
            return (finger, tail);
        }
        static double Sum(List<double> a) { double s = 0; foreach (var v in a) s += v; return s; }

        int nfull = (int)Math.Max(4, JsRound(dist / 12.0));
        List<double> f = new(), tl = new();
        for (int it = 0; it < 40; it++)
        {
            double amp = dist;
            for (int j = 0; j < 6; j++)
            {
                (f, tl) = Build(nfull, amp);
                double tot = Sum(f) + Sum(tl);
                if (tot <= 0) break;
                amp *= dist / tot;
            }
            (f, tl) = Build(nfull, amp);
            if (f.Max() <= TrackpadPeakPx || nfull >= 600) break;
            nfull = (int)Math.Ceiling(nfull * 1.15);
        }
        var folded = new List<double>();
        double carry = 0;
        foreach (var v0 in f)
        {
            double v = v0 + carry;
            carry = 0;
            if (v < TrackpadPx.Lo) carry = v; else folded.Add(v);
        }
        if (carry != 0) { if (folded.Count > 0) folded[^1] += carry; else folded.Add(carry); }
        double rest = dist - Sum(folded) - Sum(tl);
        folded[^1] += rest;
        if (folded[^1] < TrackpadPx.Lo)
        {
            int k = Math.Max(1, (int)Math.Floor(dist / 3.0));
            return (Enumerable.Repeat(dist / k, k).ToList(), new List<double>());
        }
        return (folded, tl);
    }

    /// <summary>The swipes of a trackpad scroll of (dx, dy); see <see cref="PlanTrackpad"/>. Draw order as Python's.</summary>
    public static List<TrackpadSwipe> PlanTrackpadStrokes(double dx, double dy, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double d = Hypot(dx, dy);
        if (d < TrackpadPx.Lo)
            return dx != 0 || dy != 0
                ? new List<TrackpadSwipe> { new(new List<WheelEvent> { new(dx, dy, 0) }, new List<WheelEvent>(), 0) }
                : new List<TrackpadSwipe>();
        double ux = dx / d, uy = dy / d;
        double hz = Lerp(TrackpadHz.Lo, TrackpadHz.Hi, rng());
        double dt = 1000 / hz;
        int n = Math.Max(1, (int)Math.Ceiling(d / TrackpadStrokePx));
        bool pause = rng() < TrackpadPauseRate;
        double pauseMs = Lerp(TrackpadPauseMs.Lo, TrackpadPauseMs.Hi, rng());
        int pauseSwipe = Math.Min(n - 1, (int)Math.Floor(rng() * n));
        double pauseAt = Lerp(0.25, 0.75, rng());
        var specs = new List<(double Share, double TauR, double Decay, double Gap)>();
        for (int i = 0; i < n; i++)
            specs.Add((Lerp(0.9, 1.1, rng()), Lerp(0.55, 0.75, rng()), Lerp(140, 220, rng()), Lerp(TrackpadGapMs.Lo, TrackpadGapMs.Hi, rng())));
        double totalShare = 0;
        foreach (var sp in specs) totalShare += sp.Share;
        var outSwipes = new List<TrackpadSwipe>();
        double done = 0;
        for (int si = 0; si < n; si++)
        {
            var (share, tauR, decay, gap) = specs[si];
            double dist = si == n - 1 ? d - done : d * share / totalShare;
            done += dist;
            var (finger, tail) = Swipe(dist, dt, tauR, decay);
            int stall = pause && si == pauseSwipe && finger.Count > 2 ? (int)Math.Floor(pauseAt * (finger.Count - 1)) : -1;
            var fEv = new List<WheelEvent>();
            for (int i = 0; i < finger.Count; i++)
            {
                double sl = dt * Lerp(0.92, 1.08, rng());
                if (i == stall) sl += pauseMs;
                fEv.Add(new WheelEvent(ux * finger[i], uy * finger[i], sl));
            }
            var tEv = new List<WheelEvent>();
            foreach (var v in tail) tEv.Add(new WheelEvent(ux * v, uy * v, dt * Lerp(0.92, 1.08, rng())));
            outSwipes.Add(new TrackpadSwipe(fEv, tEv, si < n - 1 ? gap : 0));
        }
        return outSwipes;
    }

    /// <summary>
    /// A two-finger trackpad scroll of (dx, dy) px as wheel events in order (<c>SleepMs</c> = wait AFTER the
    /// event). Per swipe (&lt;= 900 px): 2-40 px events at 60-120/s on a min-jerk bell, lifted while moving, then
    /// an inertia tail decaying for &gt;= 300 ms until |delta| &lt; 1; 20 % of scrolls stall once 120-400 ms.
    /// </summary>
    public static List<WheelEvent> PlanTrackpad(double dx, double dy, Rng? rng = null)
    {
        var outEv = new List<WheelEvent>();
        foreach (var sw in PlanTrackpadStrokes(dx, dy, rng))
        {
            var evs = new List<WheelEvent>(sw.Finger);
            evs.AddRange(sw.Tail);
            if (evs.Count > 0 && sw.GapMs != 0) evs[^1] = evs[^1] with { SleepMs = evs[^1].SleepMs + sw.GapMs };
            outEv.AddRange(evs);
        }
        return outEv;
    }

    /// <summary>The nearest scrollable ancestor of a plain-CSS selector's first match (isolated world).
    /// Same source as Python <c>_scroll.SCROLLER</c> and scroll.ts <c>ISO_SCROLLER</c>.</summary>
    internal const string IsoScroller = @"(a) => { let e;
  try { e = document.querySelector(a.sel); } catch (x) { return null; }
  if (!e) return null;
  const top = document.scrollingElement || document.documentElement;
  for (let p = e.parentElement; p && p !== top && p !== document.body; p = p.parentElement) {
    const s = getComputedStyle(p), sc = v => v === 'auto' || v === 'scroll' || v === 'overlay';
    const y = sc(s.overflowY) && p.scrollHeight > p.clientHeight + 1;
    const x = sc(s.overflowX) && p.scrollWidth > p.clientWidth + 1;
    if (x || y) { const r = p.getBoundingClientRect();
      return { rect: [r.x + p.clientLeft, r.y + p.clientTop, p.clientWidth, p.clientHeight], x: x, y: y }; }
  }
  return null; }";

    // ---- the I/O loop ------------------------------------------------------------------------------
    private static async Task<Box?> ReadSettledAsync(Func<Task<Box?>> boxFn, IScrollIO io)
    {
        var a = await boxFn().ConfigureAwait(false);
        var t0 = Environment.TickCount64;
        while (true)
        {
            await io.SleepAsync(SettleMs).ConfigureAwait(false);
            var b = await boxFn().ConfigureAwait(false);
            if (a is not Box av || b is not Box bv) return b;
            if (Math.Abs(av.X - bv.X) < 0.01 && Math.Abs(av.Y - bv.Y) < 0.01) return b;
            if (Environment.TickCount64 - t0 > SettleCapMs) return b;
            a = b;
        }
    }

    private static async Task SendNotchesAsync(IScrollIO io, int n, bool axisX, int sign, Rng rng)
    {
        int notch = await io.NotchPxAsync().ConfigureAwait(false);
        int burst = (int)Math.Floor(Lerp(FlickNotches.Lo, FlickNotches.Hi + 1, rng()));
        for (int k = 0; k < n; k++)
        {
            await io.WheelAsync(axisX ? sign * notch : 0, axisX ? 0 : sign * notch).ConfigureAwait(false);
            if (k == n - 1) break;
            if (--burst == 0)
            {
                await io.SleepAsync(Lerp(FlickGapMs.Lo, FlickGapMs.Hi, rng())).ConfigureAwait(false);
                burst = (int)Math.Floor(Lerp(FlickNotches.Lo, FlickNotches.Hi + 1, rng()));
            }
            else await io.SleepAsync(Lerp(NotchGapMs.Lo, NotchGapMs.Hi, rng())).ConfigureAwait(false);
            if (rng() < ReadPauseRate) await io.SleepAsync(Lerp(ReadPauseMs.Lo, ReadPauseMs.Hi, rng())).ConfigureAwait(false);
        }
    }

    private static async Task SendTrackpadAsync(IScrollIO io, double dx, double dy, Rng rng)
    {
        foreach (var e in PlanTrackpad(dx, dy, rng))
        {
            await io.WheelAsync(e.Dx, e.Dy).ConfigureAwait(false);
            if (e.SleepMs > 0) await io.SleepAsync(e.SleepMs).ConfigureAwait(false);
        }
    }

    private static async Task<Box> ViewOfAsync(IScrollIO io, Scroller? sc)
    {
        var (vw, vh) = await io.ViewportAsync().ConfigureAwait(false);
        if (sc is null) return new Box(0, 0, vw, vh);
        double x0 = Math.Max(0, sc.X), y0 = Math.Max(0, sc.Y);
        double x1 = Math.Min(vw, sc.X + sc.Width), y1 = Math.Min(vh, sc.Y + sc.Height);
        return new Box(x0, y0, Math.Max(0, x1 - x0), Math.Max(0, y1 - y0));
    }

    private static async Task PlaceOverAsync(IScrollIO io, Box view, Rng rng)
    {
        if (view.Width <= 0 || view.Height <= 0) return;
        var (px, py) = io.Pointer;
        if (!AnchorNeeded(px - view.X, py - view.Y, view.Width, view.Height, io.Placed, io.Held)) return;
        var (ax, ay) = AnchorPoint(view.Width, view.Height, rng);
        await io.GlideAsync(view.X + ax, view.Y + ay, AnchorTargetW).ConfigureAwait(false);
        await io.SleepAsync(Lerp(60, 180, rng())).ConfigureAwait(false);   // eyes land before the finger rolls the wheel
    }

    /// <summary>
    /// Wheel the target whose current box <paramref name="boxFn"/> returns into view (see Python
    /// <c>bring_into_view</c>): anchor (or over the nearest scrollable ancestor; an off-screen container is
    /// wheeled in first), whole notches or a trackpad swipe for 60-90 % of the need, settle (two equal reads
    /// 50 ms apart), re-read, correct; at most 60 notches. Ok false: the wheel did not move it or the budget ran out.
    /// </summary>
    internal static async Task<(Box? Box, bool Ok, int Notches)> BringIntoViewAsync(IScrollIO io, Func<Task<Box?>> boxFn,
        double? frac = null, Rng? rng = null, Func<Task<Scroller?>>? scrollerFn = null)
    {
        Rng r = rng ?? io.Rng;
        var box = await boxFn().ConfigureAwait(false);
        if (box is null) return (null, false, 0);
        var scroller = scrollerFn is null ? null : await scrollerFn().ConfigureAwait(false);
        if (scroller is not null)
        {
            var (vw0, vh0) = await io.ViewportAsync().ConfigureAwait(false);
            if (!InView(new Box(scroller.X, scroller.Y, scroller.Width, scroller.Height), new Box(0, 0, vw0, vh0)))
            {
                async Task<Box?> SRect()
                {
                    var s = await scrollerFn!().ConfigureAwait(false);
                    return s is null ? null : new Box(s.X, s.Y, s.Width, s.Height);
                }
                var first0 = await BringIntoViewAsync(io, SRect, frac, r, null).ConfigureAwait(false);
                if (!first0.Ok) return (await boxFn().ConfigureAwait(false), false, 0);
                scroller = await scrollerFn!().ConfigureAwait(false);
                box = await boxFn().ConfigureAwait(false);
                if (box is null) return (null, false, 0);
            }
        }
        var view = await ViewOfAsync(io, scroller).ConfigureAwait(false);
        if (InView(box.Value, view)) return (box, true, 0);
        double f = frac ?? TargetFrac(r);
        int notches = 0;
        bool first = true, placed = false;
        string device = await io.DeviceAsync().ConfigureAwait(false);
        int notch = await io.NotchPxAsync().ConfigureAwait(false);
        var need0 = ScrollNeed(box.Value, view, f);
        bool ax = need0.Dx != 0, ay = need0.Dy != 0;
        if (scroller is not null) { ax = ax && scroller.AxisX; ay = ay && scroller.AxisY; }
        double tol = device != "trackpad" ? notch * 0.5 : 6;
        int shownRounds = 0;
        while (notches < NotchBudget)
        {
            var (dx, dy) = AimNeed(box.Value, view, f, ax, ay);
            if (InView(box.Value, view))
            {
                if ((Math.Abs(dx) < tol && Math.Abs(dy) < tol) || shownRounds >= 2) return (box, true, notches);
                shownRounds++;
            }
            else if (Math.Abs(dx) < 1 && Math.Abs(dy) < 1) return (box, false, notches);
            if (!placed) { await PlaceOverAsync(io, view, r).ConfigureAwait(false); placed = true; }
            double askX = first ? Undershoot(dx, r) : dx;
            double askY = first ? Undershoot(dy, r) : dy;
            first = false;
            if (device == "trackpad")
            {
                await SendTrackpadAsync(io, askX, askY, r).ConfigureAwait(false);
                notches += (int)Math.Max(1, JsRound(Hypot(askX, askY) / notch));
            }
            else
            {
                int ny = Math.Abs(askY) >= tol ? Motion.WheelNotches(askY, notch) : 0;
                int nx = Math.Abs(askX) >= tol ? Motion.WheelNotches(askX, notch) : 0;
                ny = Math.Min(ny, NotchBudget - notches);
                if (ny > 0) { await SendNotchesAsync(io, ny, false, askY > 0 ? 1 : -1, r).ConfigureAwait(false); notches += ny; }
                nx = Math.Min(nx, NotchBudget - notches);
                if (nx > 0) { await SendNotchesAsync(io, nx, true, askX > 0 ? 1 : -1, r).ConfigureAwait(false); notches += nx; }
                if (nx == 0 && ny == 0) return (box, InView(box.Value, view), notches);
            }
            var before = box.Value;
            box = await ReadSettledAsync(boxFn, io).ConfigureAwait(false);
            if (box is null) return (null, false, notches);
            if (scroller is not null)
            {
                scroller = scrollerFn is null ? scroller : await scrollerFn().ConfigureAwait(false);
                view = await ViewOfAsync(io, scroller).ConfigureAwait(false);
            }
            if (Math.Abs(box.Value.X - before.X) < MinMovePx && Math.Abs(box.Value.Y - before.Y) < MinMovePx)
                return (box, InView(box.Value, view), notches);
        }
        return (box, false, notches);
    }
}
