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
/// changing button state. Never a teleport: a skip that would put more than <see cref="MaxJumpPx"/>
/// between two dispatched points dispatches the in-between sample instead. Sleeps are local timers,
/// never a protocol round trip. Nothing here changes what the planner returns.
/// </summary>
internal static class Pacing
{
    public const double MaxJumpPx = 40;

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
        double t0 = now();
        int i = 0, sent = 0;
        (double X, double Y)? last = null;
        while (i < steps.Count)
        {
            double el = now() - t0;
            if (el < due[i]) await sleep(due[i] - el).ConfigureAwait(false);
            else i = NextIndex(steps, due, i, last, el, maxJump, pinned);
            var s = steps[i];
            await move(s.X, s.Y).ConfigureAwait(false);
            sent++;
            last = (s.X, s.Y);
            i++;
        }
        double end = now() - t0;
        if (end < total) await sleep(total - end).ConfigureAwait(false);   // the last sample's own dwell
        return sent;
    }
}
