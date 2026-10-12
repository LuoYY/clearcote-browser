using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading.Tasks;

namespace Clearcote;

/// <summary>
/// Wall-clock pacing for planned pointer paths (mirrors sdk/python/clearcote/_pacing.py and
/// sdk/node/src/pacing.ts).
///
/// The planner (<see cref="Motion.PlanMove"/>) returns samples at the persona's device rate (~6-8 ms
/// apart). Walking them as "dispatch, then sleep the planned gap" adds the dispatch round trip to
/// EVERY sample: measured on PRO r38, one mouse move costs 27 ms median, so a 1.05 s planned glide took
/// 3.5-5.4 s and the page saw ~25 pointermove/s (SC 209). On Windows Task.Delay also rounds every
/// 6-8 ms gap up to the ~15.6 ms timer tick, which stretched the same glide on its own.
///
/// <see cref="WalkAsync"/> schedules each sample at its planned offset from the start and keeps that
/// clock: on time it sleeps until the sample's planned time; behind, it skips the samples whose time has
/// passed and dispatches the most recent due one. Never skipped: the final sample and a sample marked as
/// changing button state. Never a teleport: a skip may not hop further than a fast hand covers in the time
/// between two dispatches (<see cref="FastPxPerMs"/>, at least <see cref="MaxJumpPx"/>); a longer hop
/// dispatches the in-between sample instead. A stall is a pause: a single
/// dispatch slower than <see cref="StallMs"/> shifts the schedule by the excess instead of being caught up
/// by skipping,. Sleeps are local timers,
/// never a protocol round trip. Nothing here changes what the planner returns.
/// </summary>
internal static class Pacing
{
    public const double MaxJumpPx = 40;

    /// <summary>A single dispatch slower than this is a stall (the page or the box was busy), not latency:
    /// nothing reached the page while it lasted, so the plan pauses with it instead of skipping ahead.</summary>
    public const double StallMs = 60;

    /// <summary>A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s). A skip may hop as far
    /// as that covers in the time since the last dispatch, and never less than <see cref="MaxJumpPx"/>.</summary>
    public const double FastPxPerMs = 3;

    /// <summary>Planned dispatch offsets (ms from the start) of every step, and the planned total.</summary>
    public static (double[] Due, double Total) Schedule(IReadOnlyList<Step> steps)
    {
        var due = new double[steps.Count];
        double t = 0;
        for (int i = 0; i < steps.Count; i++) { due[i] = t; t += Math.Max(0, steps[i].SleepMs); }
        return (due, t);
    }

    /// <summary>The step to dispatch next: step <paramref name="i"/> is the first not yet sent.</summary>
    public static int NextIndex(IReadOnlyList<Step> steps, double[] due, int i, (double X, double Y)? last,
        double elapsedMs, double maxJump = MaxJumpPx, Func<int, bool>? pinned = null)
    {
        int n = steps.Count, j = i;
        while (j + 1 < n && due[j + 1] <= elapsedMs && !(pinned?.Invoke(j) ?? false)) j++;
        if (last is { } l)
            while (j > i && Math.Sqrt(Sq(steps[j].X - l.X) + Sq(steps[j].Y - l.Y)) > maxJump) j--;
        return j;
    }

    private static double Sq(double v) => v * v;

    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static double RealNow() => Clock.Elapsed.TotalMilliseconds;
    private static Task RealSleep(double ms) => ms > 0 ? Task.Delay(TimeSpan.FromMilliseconds(ms)) : Task.CompletedTask;

    /// <summary>
    /// Dispatch <paramref name="steps"/> through <paramref name="move"/> against a wall clock. A move
    /// that throws propagates (the caller decides what a closed page means). Returns the number of
    /// samples dispatched.
    /// </summary>
    public static async Task<int> WalkAsync(IReadOnlyList<Step> steps, Func<double, double, Task> move,
        Func<double>? now = null, Func<double, Task>? sleep = null, double maxJump = MaxJumpPx,
        Func<int, bool>? pinned = null)
    {
        now ??= RealNow;
        sleep ??= RealSleep;
        if (steps.Count == 0) return 0;
        var (due, total) = Schedule(steps);
        double t0 = now(), tLast = t0;
        int i = 0, sent = 0;
        (double X, double Y)? last = null;
        while (i < steps.Count)
        {
            double el = now() - t0;
            if (el < due[i]) await sleep(due[i] - el).ConfigureAwait(false);
            else i = NextIndex(steps, due, i, last, el, JumpCap(maxJump, now() - tLast), pinned);
            var s = steps[i];
            double tb = now();
            await move(s.X, s.Y).ConfigureAwait(false);
            sent++;
            // Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping
            // coarsened the rest of the glide to maximum hops, and across the fixture's iframe the page saw
            // a 110 px jump. A stall pauses the plan; only StallMs of it is caught up.
            double took = now() - tb;
            if (took > StallMs) t0 += took - StallMs;
            last = (s.X, s.Y);
            tLast = tb;
            i++;
        }
        double end = now() - t0;
        if (end < total) await sleep(total - end).ConfigureAwait(false);   // the last sample's own dwell
        return sent;
    }

    /// <summary>The largest hop allowed between two dispatched points <paramref name="dtMs"/> apart: what a
    /// fast hand covers in that time, never less than <paramref name="maxJump"/>.</summary>
    public static double JumpCap(double maxJump, double dtMs) => Math.Max(maxJump, FastPxPerMs * Math.Max(0, dtMs));
}
