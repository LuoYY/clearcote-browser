using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;

namespace Clearcote;

// Detector-emulator: the feature vector a behavioural collector would compute from an input stream and a
// 0..1 "how human" score with the penalty names that fired. Port of sdk/python/clearcote/_motionscore.py and
// sdk/node/src/motionscore.ts (SC 232 lane G): ScoreMotion for pointer paths, plus typed runs, wheel streams,
// drags, hover dwell and ambient motion behind one ScoreAction(kind, samples). Same thresholds, same penalty
// names; sdk/testdata/humanize_motion_vectors.json `scores` pins the three SDKs to identical scores.
//
// A VALIDATION tool and a relative signal (old vs new), not an absolute pass/fail: real collectors' models are
// private. Pure: no Playwright, no I/O.

/// <summary>A pointer sample: position (CSS px), time (ms) and the buttons bitmask (1 = primary held).</summary>
public readonly record struct MotionSample(double X, double Y, double T, int Buttons = 0);

/// <summary>One key of a typed run: keydown and keyup times (ms).</summary>
public readonly record struct KeySample(string Key, double Down, double Up);

/// <summary>One wheel event: time (ms), deltas and deltaMode (0 = pixels, 1 = lines).</summary>
public readonly record struct WheelSample(double T, double Dy, double Dx = 0, int Mode = 0);

/// <summary>Score 0..1 (higher = more human), the penalties that fired, and the features they came from.</summary>
public sealed record ActionScore(string Kind, double Score, IReadOnlyList<string> Flags, IReadOnlyDictionary<string, object?> Features);

public static class MotionScore
{
    private static string F(double v, int d) => v.ToString("F" + d, CultureInfo.InvariantCulture);

    private static double Mean(IReadOnlyList<double> a) => a.Count == 0 ? 0 : a.Sum() / a.Count;

    private static double Std(IReadOnlyList<double> a)
    {
        if (a.Count < 2) return 0;
        double m = Mean(a);
        return Math.Sqrt(a.Select(x => (x - m) * (x - m)).Sum() / a.Count);
    }

    private static double Cv(IReadOnlyList<double> a) { double m = Mean(a); return m > 1e-9 ? Std(a) / m : 0; }

    private static double Median(IReadOnlyList<double> a)
    {
        if (a.Count == 0) return 0;
        var s = a.OrderBy(x => x).ToArray();
        int m = s.Length / 2;
        return s.Length % 2 == 1 ? s[m] : (s[m - 1] + s[m]) / 2;
    }

    private static List<double> Smooth(IReadOnlyList<double> a, int w)
    {
        var o = new List<double>(a.Count);
        for (int i = 0; i < a.Count; i++)
        {
            double s = 0; int c = 0;
            for (int j = Math.Max(0, i - w); j <= Math.Min(a.Count - 1, i + w); j++) { s += a[j]; c++; }
            o.Add(s / c);
        }
        return o;
    }

    private static double Hyp(double dx, double dy) => Math.Sqrt(dx * dx + dy * dy);

    private sealed class Pen
    {
        public double Score = 1;
        public readonly List<string> Flags = new();
        public void P(bool cond, double amt, string flag) { if (cond) { Score -= amt; Flags.Add(flag); } }
    }

    private static ActionScore Done(string kind, double score, List<string> flags, IReadOnlyDictionary<string, object?> f) =>
        new(kind, Math.Max(0, Math.Round(score, 4, MidpointRounding.ToEven)), flags, f);

    // ------------------------------------------------------------------ pointer paths (port of extract_features)

    /// <summary>The pointer-path feature vector (see _motionscore.MotionFeatures for each field).</summary>
    public static Dictionary<string, object?> ExtractFeatures(IReadOnlyList<MotionSample> s)
    {
        int n = s.Count;
        double duration = n > 1 ? s[n - 1].T - s[0].T : 0;
        var v = new List<double>(); var tMid = new List<double>();
        for (int i = 1; i < n; i++)
        {
            double dt = Math.Max(1e-3, s[i].T - s[i - 1].T);
            v.Add(Hyp(s[i].X - s[i - 1].X, s[i].Y - s[i - 1].Y) / dt);
            tMid.Add((s[i].T + s[i - 1].T) / 2);
        }
        var sv = Smooth(v, 2);
        int peak = 0;
        for (int i = 1; i < sv.Count; i++) if (sv[i] > sv[peak]) peak = i;
        double peakFrac = duration > 0 && sv.Count > 0 ? (tMid[peak] - s[0].T) / duration : 0;
        double vmean = Mean(sv);
        double vstd = Std(sv);
        // constant speed: std 0 up to float noise, whose skew is meaningless (reads as 0 in every SDK)
        double skew = sv.Count > 0 && vstd > 1e-9 * Math.Max(1, Math.Abs(vmean)) ? sv.Select(x => Math.Pow((x - vmean) / vstd, 3)).Sum() / sv.Count : 0;
        double vmax = sv.Count == 0 ? 1e-9 : Math.Max(sv.Max(), 1e-9);
        int sub = 0, lastMin = -10;
        for (int i = 2; i < sv.Count - 2; i++)
        {
            bool valley = sv[i] < sv[i - 1] && sv[i] <= sv[i + 1] && sv[i] < sv[i - 2] && sv[i] < sv[i + 2];
            if (valley && sv[i] < 0.5 * vmax && i - lastMin >= 3) { sub++; lastMin = i; }
        }
        double pathLen = 0;
        for (int i = 1; i < n; i++) pathLen += Hyp(s[i].X - s[i - 1].X, s[i].Y - s[i - 1].Y);
        double net = n > 1 ? Hyp(s[n - 1].X - s[0].X, s[n - 1].Y - s[0].Y) : 0;
        double straight = pathLen > 1e-6 ? net / pathLen : 1;

        double mjr = 1;
        if (sv.Count >= 4 && duration > 0)
        {
            double area = 0;
            for (int i = 0; i < sv.Count; i++) area += sv[i] * (i == 0 ? 0 : tMid[i] - tMid[i - 1]);
            if (area == 0) area = 1;
            double res = 0, norm = 0;
            for (int i = 0; i < sv.Count; i++)
            {
                double tau = (tMid[i] - s[0].T) / duration;
                double ideal = 30 * tau * tau * (1 - tau) * (1 - tau);
                double obs = sv[i] * duration / area;
                res += (obs - ideal) * (obs - ideal);
                norm += ideal * ideal;
            }
            mjr = norm > 0 ? Math.Sqrt(res / norm) : 1;
        }

        var dts = new List<double>();
        for (int i = 1; i < n; i++) dts.Add(s[i].T - s[i - 1].T);
        var sorted = dts.OrderBy(x => x).ToList();
        double med = sorted.Count > 0 ? sorted[sorted.Count / 2] : 0;
        double core = med > 0 ? (double)dts.Count(d => d >= 0.8 * med && d <= 1.2 * med) / dts.Count : 0;
        bool dtUniform = core < 0.5;

        double jac = 0;
        if (n >= 8)
        {
            var xs = s.Select(p => p.X).ToList(); var ys = s.Select(p => p.Y).ToList();
            var sx = Smooth(xs, 3); var sy = Smooth(ys, 3);
            var rx = xs.Select((x, i) => x - sx[i]).ToList(); var ry = ys.Select((y, i) => y - sy[i]).ToList();
            static double Ac(List<double> r)
            {
                double num = 0, den = 0;
                for (int i = 1; i < r.Count; i++) num += r[i] * r[i - 1];
                for (int i = 0; i < r.Count; i++) den += r[i] * r[i];
                return den > 1e-9 ? num / den : 0;
            }
            jac = (Ac(rx) + Ac(ry)) / 2;
        }

        double plr = 0;
        if (n >= 6)
        {
            var lv = new List<double>(); var lc = new List<double>();
            for (int i = 1; i < n - 1; i++)
            {
                double ax = s[i].X - s[i - 1].X, ay = s[i].Y - s[i - 1].Y, bx = s[i + 1].X - s[i].X, by = s[i + 1].Y - s[i].Y;
                double cross = Math.Abs(ax * by - ay * bx);
                double sa = Hyp(ax, ay), sb = Hyp(bx, by);
                double curv = cross / (sa * sb * Hyp(bx + ax, by + ay) + 1e-9);
                double sp = (sa + sb) / 2 / Math.Max(1e-3, (s[i + 1].T - s[i - 1].T) / 2);
                if (curv > 1e-6 && sp > 1e-6) { lv.Add(Math.Log(sp)); lc.Add(Math.Log(curv)); }
            }
            if (lv.Count >= 4)
            {
                double mv = Mean(lv), mc = Mean(lc), num = 0, dv = 0, dc = 0;
                for (int i = 0; i < lv.Count; i++) { num += (lv[i] - mv) * (lc[i] - mc); dv += (lv[i] - mv) * (lv[i] - mv); dc += (lc[i] - mc) * (lc[i] - mc); }
                plr = dv > 0 && dc > 0 ? num / Math.Sqrt(dv * dc) : 0;
            }
        }

        double? grab = null, release = null;
        var held = Enumerable.Range(0, n).Where(i => (s[i].Buttons & 1) != 0).ToList();
        if (held.Count > 0)
        {
            int d0 = held[0];
            double firstMove = s[d0].T;
            foreach (int i in held) if (Hyp(s[i].X - s[d0].X, s[i].Y - s[d0].Y) > 1) { firstMove = s[i].T; break; }
            grab = firstMove - s[d0].T;
            int lastMove = d0;
            for (int k = 1; k < held.Count; k++)
                if (Hyp(s[held[k]].X - s[held[k - 1]].X, s[held[k]].Y - s[held[k - 1]].Y) > 1) lastMove = held[k];
            int lh = held[^1];
            double upT = lh + 1 < n ? s[lh + 1].T : s[lh].T;
            release = upT - s[lastMove].T;
        }

        return new Dictionary<string, object?>
        {
            ["n"] = n, ["duration_ms"] = duration, ["peak_vel_frac"] = peakFrac, ["vel_skew"] = skew,
            ["submovements"] = sub, ["straightness"] = straight, ["min_jerk_residual"] = mjr,
            ["dt_mean_ms"] = Mean(dts), ["dt_std_ms"] = Std(dts), ["dt_uniform"] = dtUniform,
            ["jitter_autocorr"] = jac, ["power_law_r"] = plr, ["grab_ms"] = grab, ["release_ms"] = release,
        };
    }

    private static double D(IReadOnlyDictionary<string, object?> f, string k) => Convert.ToDouble(f[k], CultureInfo.InvariantCulture);

    /// <summary>Pointer path score (port of score_motion). <paramref name="drag"/> adds the grab/release checks.</summary>
    public static ActionScore ScoreMotion(IReadOnlyList<MotionSample> s, bool drag = false)
    {
        var f = ExtractFeatures(s);
        var p = new Pen();
        double pf = D(f, "peak_vel_frac"), sk = D(f, "vel_skew"), ja = D(f, "jitter_autocorr");
        int sub = (int)f["submovements"]!;
        p.P(pf < 0.15, 0.15, $"peak-velocity-ease-out({F(pf, 2)})");
        p.P(pf > 0.48, 0.12, $"peak-velocity-too-symmetric({F(pf, 2)})");
        p.P(sk < 0.1, 0.12, $"velocity-symmetric(skew={F(sk, 2)})");
        p.P(sub < 1, 0.2, "no-corrective-submovement");
        p.P(sub > 4, 0.12, $"too-many-submovements({sub})");
        p.P((bool)f["dt_uniform"]!, 0.15, "uniform-dt-cadence");
        p.P(ja < 0.1, 0.1, $"white-noise-jitter({F(ja, 2)})");
        p.P(D(f, "straightness") > 0.9995, 0.08, "perfectly-straight-path");
        if (drag)
        {
            var g = (double?)f["grab_ms"]; var r = (double?)f["release_ms"];
            p.P(g is { } gv && gv < 60, 0.2, $"grab-hesitation-too-short({F(g ?? 0, 1)}ms)");
            p.P(r is { } rv && rv < 40, 0.2, $"release-delay-too-short({F(r ?? 0, 1)}ms)");
        }
        return new ActionScore("glide", Math.Max(0, p.Score), p.Flags, f);
    }

    // ------------------------------------------------------------------ typed runs

    private static readonly Dictionary<char, (int Hand, int Finger)> Fingers = BuildFingers();

    private static Dictionary<char, (int, int)> BuildFingers()
    {
        var m = new Dictionary<char, (int, int)>();
        var rows = new (int Hand, string[] Keys)[] { (0, new[] { "qaz1", "wsx2", "edc3", "rfvtgb45" }), (1, new[] { "yhnujm67", "ik,8", "ol.9", "p;/[]'0-=" }) };
        foreach (var (hand, keys) in rows)
            for (int f = 0; f < keys.Length; f++)
                foreach (char c in keys[f]) m[c] = (hand, hand == 0 ? f : 3 - f);
        m[' '] = (2, 4);
        return m;
    }

    /// <summary>(hand, finger) for a typed key on a QWERTY layout: hand 0 left / 1 right / 2 thumb (space);
    /// finger 0 pinky .. 3 index. Null for keys off the letter block.</summary>
    public static (int Hand, int Finger)? FingerOf(string? key)
    {
        if (string.IsNullOrEmpty(key)) return null;
        string k = key.ToLowerInvariant();
        if (k == "space") k = " ";
        if (k.Length != 1) return null;
        return Fingers.TryGetValue(k[0], out var v) ? v : null;
    }

    public static Dictionary<string, object?> TypedFeatures(IReadOnlyList<KeySample> s)
    {
        var ev = s.OrderBy(e => e.Down).ToList();   // stable, like Python sorted / JS sort
        int n = ev.Count;
        var gaps = new List<double>();
        for (int i = 1; i < n; i++) gaps.Add(ev[i].Down - ev[i - 1].Down);
        var dwells = ev.Select(e => Math.Max(0, e.Up - e.Down)).ToList();
        int roll = 0;
        for (int i = 1; i < n; i++) if (ev[i].Down < ev[i - 1].Up) roll++;
        var same = new List<double>(); var alt = new List<double>();
        for (int i = 1; i < n; i++)
        {
            var a = FingerOf(ev[i - 1].Key); var b = FingerOf(ev[i].Key);
            if (a is null || b is null || a.Value.Hand == 2 || b.Value.Hand == 2) continue;
            if (a.Value == b.Value) same.Add(gaps[i - 1]);
            else if (a.Value.Hand != b.Value.Hand) alt.Add(gaps[i - 1]);
        }
        double? ratio = same.Count >= 3 && alt.Count >= 3 && Median(alt) > 0 ? Median(same) / Median(alt) : null;
        return new Dictionary<string, object?>
        {
            ["n"] = n, ["gap_mean_ms"] = Mean(gaps), ["gap_cv"] = gaps.Count >= 2 ? Cv(gaps) : 0.0,
            ["gap_min_ms"] = gaps.Count > 0 ? gaps.Min() : 0.0, ["dwell_mean_ms"] = Mean(dwells),
            ["dwell_cv"] = dwells.Count >= 2 ? Cv(dwells) : 0.0,
            ["dwell_band_frac"] = dwells.Count > 0 ? (double)dwells.Count(d => d >= 30 && d <= 200) / dwells.Count : 0.0,
            ["rollover_rate"] = n > 1 ? (double)roll / (n - 1) : 0.0, ["same_finger_n"] = same.Count,
            ["alternating_n"] = alt.Count, ["same_finger_ratio"] = ratio,
        };
    }

    public static ActionScore ScoreTyped(IReadOnlyList<KeySample> s)
    {
        var f = TypedFeatures(s);
        var p = new Pen();
        int n = (int)f["n"]!;
        double cv = D(f, "gap_cv"), gmin = D(f, "gap_min_ms"), band = D(f, "dwell_band_frac"), dcv = D(f, "dwell_cv"), rr = D(f, "rollover_rate");
        var ratio = (double?)f["same_finger_ratio"];
        p.P(n >= 3 && cv < 0.15, 0.25, $"key-gap-too-regular(cv={F(cv, 2)})");
        p.P(n >= 3 && cv > 1.6, 0.10, $"key-gap-erratic(cv={F(cv, 2)})");
        p.P(n >= 3 && gmin < 8, 0.15, $"key-gap-near-zero({F(gmin, 0)}ms)");
        p.P(n >= 2 && band < 0.7, 0.15, $"dwell-out-of-band({F(band, 2)} in 30-200ms)");
        p.P(n >= 3 && dcv < 0.08, 0.15, $"dwell-constant(cv={F(dcv, 2)})");
        p.P(ratio is { } r && r < 1.05, 0.15, $"no-bigram-structure(same/alt={F(ratio ?? 0, 2)})");
        p.P(n >= 15 && rr == 0, 0.10, "no-rollover");
        p.P(n >= 5 && rr > 0.6, 0.10, $"too-much-rollover({F(rr, 2)})");
        return Done("typed", p.Score, p.Flags, f);
    }

    // ------------------------------------------------------------------ wheel streams

    private static readonly double[] NotchPx = { 40, 100, 120 };

    public static Dictionary<string, object?> WheelFeatures(IReadOnlyList<WheelSample> s)
    {
        var ev = s.OrderBy(e => e.T).ToList();
        int n = ev.Count;
        var mags = ev.Select(e => e.Dy != 0 ? Math.Abs(e.Dy) : Math.Abs(e.Dx)).ToList();
        var gaps = new List<double>();
        for (int i = 1; i < n; i++) gaps.Add(ev[i].T - ev[i - 1].T);
        int distinct = mags.Select(m => Math.Round(m, 3)).Distinct().Count();
        bool line = n > 0 && ev.All(e => e.Mode == 1);
        bool notch = n > 0 && (line || (distinct == 1 && NotchPx.Contains(mags[0])));
        var bursts = new List<int>(); var intra = new List<double>();
        if (n > 0)
        {
            int size = 1;
            foreach (var g in gaps) { if (g >= 90) { bursts.Add(size); size = 1; } else { intra.Add(g); size++; } }
            bursts.Add(size);
        }
        var tail = n >= 6 ? mags.Skip(Math.Max(1, 2 * n / 3)).ToList() : new List<double>();
        int dec = 0;
        for (int i = 1; i < tail.Count; i++) if (tail[i] <= tail[i - 1]) dec++;
        double gm = Median(gaps);
        return new Dictionary<string, object?>
        {
            ["n"] = n, ["notch"] = notch, ["line_mode"] = line, ["distinct_deltas"] = distinct,
            ["delta_median_px"] = Median(mags), ["delta_max_px"] = mags.Count > 0 ? mags.Max() : 0.0,
            ["gap_median_ms"] = gm, ["gap_cv"] = gaps.Count >= 2 ? Cv(gaps) : 0.0,
            ["intra_burst_median_ms"] = Median(intra), ["burst_sizes"] = bursts, ["flick_resets"] = Math.Max(0, bursts.Count - 1),
            ["rate_hz"] = gaps.Count > 0 && gm > 0 ? 1000 / gm : 0.0,
            ["tail_decreasing_frac"] = tail.Count >= 3 ? (double)dec / (tail.Count - 1) : 0.0,
        };
    }

    public static ActionScore ScoreWheel(IReadOnlyList<WheelSample> s)
    {
        var f = WheelFeatures(s);
        var p = new Pen();
        int n = (int)f["n"]!;
        if (n < 2) return Done("wheel", p.Score, p.Flags, f);
        double cv = D(f, "gap_cv"), gm = D(f, "gap_median_ms");
        if ((bool)f["notch"]!)
        {
            var bs = (List<int>)f["burst_sizes"]!;
            p.P(n >= 4 && cv < 0.10, 0.25, $"wheel-metronome(cv={F(cv, 2)})");
            p.P(n >= 8 && (int)f["flick_resets"]! == 0, 0.15, "no-flick-bursts");
            p.P(bs.Count > 0 && bs.Max() > 7, 0.10, $"burst-too-long({(bs.Count > 0 ? bs.Max() : 0)})");
            double im = D(f, "intra_burst_median_ms");
            p.P(im > 0 && !(im >= 10 && im <= 80), 0.15, $"wheel-cadence-off({F(im, 0)}ms)");
            p.P(gm < 4, 0.25, "wheel-same-tick");
        }
        else
        {
            int distinct = (int)f["distinct_deltas"]!;
            double mx = D(f, "delta_max_px"), hz = D(f, "rate_hz"), td = D(f, "tail_decreasing_frac");
            p.P(distinct <= 1, 0.20, "trackpad-constant-delta");
            p.P(mx > 60, 0.15, $"trackpad-delta-too-large({F(mx, 0)}px)");
            p.P(n >= 6 && !(hz >= 45 && hz <= 150), 0.15, $"trackpad-rate-off({F(hz, 0)}Hz)");
            p.P(n >= 6 && td < 0.6, 0.20, $"no-inertia-tail({F(td, 2)})");
            p.P(n <= 8 && distinct == n && gm >= 25, 0.25, "wheel-eased-slices");
        }
        return Done("wheel", p.Score, p.Flags, f);
    }

    // ------------------------------------------------------------------ drags, hover, ambient

    public static Dictionary<string, object?> DragFeatures(IReadOnlyList<MotionSample> s)
    {
        var held = Enumerable.Range(0, s.Count).Where(i => (s[i].Buttons & 1) != 0).ToList();
        if (held.Count == 0) return new Dictionary<string, object?> { ["held_n"] = 0 };
        var p0 = s[held[0]];
        double? hes = null;
        foreach (int i in held) if (Hyp(s[i].X - p0.X, s[i].Y - p0.Y) >= 8) { hes = s[i].T - p0.T; break; }
        hes ??= s[held[^1]].T - p0.T;
        double path = 0;
        for (int k = 1; k < held.Count; k++) path += Hyp(s[held[k]].X - s[held[k - 1]].X, s[held[k]].Y - s[held[k - 1]].Y);
        var last = s[held[^1]];
        double net = Hyp(last.X - p0.X, last.Y - p0.Y);
        int lastSig = held[0];
        for (int k = 1; k < held.Count; k++)
            if (Hyp(s[held[k]].X - s[held[k - 1]].X, s[held[k]].Y - s[held[k - 1]].Y) > 1) lastSig = held[k];
        int li = held[^1];
        double upT = li + 1 < s.Count ? s[li + 1].T : s[li].T;
        return new Dictionary<string, object?>
        {
            ["held_n"] = held.Count, ["start_hesitation_ms"] = hes.Value, ["held_straightness"] = path > 1e-6 ? net / path : 1.0,
            ["settle_ms"] = upT - s[lastSig].T, ["held_ms"] = last.T - p0.T,
        };
    }

    public static ActionScore ScoreDrag(IReadOnlyList<MotionSample> s)
    {
        var m = ScoreMotion(s, drag: true);
        var f = DragFeatures(s);
        var feats = new Dictionary<string, object?>(m.Features);
        foreach (var kv in f) feats[kv.Key] = kv.Value;
        var p = new Pen { Score = m.Score };
        p.Flags.AddRange(m.Flags);
        int hn = (int)f["held_n"]!;
        if (hn == 0) { p.P(true, 0.5, "drag-no-held-span"); return Done("drag", p.Score, p.Flags, feats); }
        double hes = D(f, "start_hesitation_ms"), st = D(f, "held_straightness"), settle = D(f, "settle_ms"), hm = D(f, "held_ms");
        p.P(hes < 60, 0.20, $"drag-start-no-hesitation({F(hes, 0)}ms)");
        p.P(hes > 800, 0.05, $"drag-start-too-slow({F(hes, 0)}ms)");
        p.P(hn >= 6 && st > 0.9995, 0.10, "drag-ruler-straight");
        p.P(settle < 40, 0.15, $"drag-no-settle({F(settle, 0)}ms)");
        p.P(hn < 10 && hm > 100, 0.15, $"drag-too-few-held-moves({hn})");
        return Done("drag", p.Score, p.Flags, feats);
    }

    public static Dictionary<string, object?> HoverFeatures(IReadOnlyList<MotionSample> s)
    {
        int n = s.Count;
        if (n == 0) return new Dictionary<string, object?> { ["n"] = 0, ["dwell_ms"] = 0.0, ["after_n"] = 0, ["excursion_px"] = 0.0, ["drift_px"] = 0.0 };
        var a = s[0];
        double exc = s.Max(p => Hyp(p.X - a.X, p.Y - a.Y));
        return new Dictionary<string, object?>
        {
            ["n"] = n, ["dwell_ms"] = s[n - 1].T - a.T, ["after_n"] = n - 1, ["excursion_px"] = exc,
            ["drift_px"] = Hyp(s[n - 1].X - a.X, s[n - 1].Y - a.Y),
        };
    }

    public static ActionScore ScoreHover(IReadOnlyList<MotionSample> s)
    {
        var f = HoverFeatures(s);
        var p = new Pen();
        int after = (int)f["after_n"]!;
        double dw = D(f, "dwell_ms"), exc = D(f, "excursion_px");
        p.P(after < 3, 0.30, $"hover-no-dwell-samples({after})");
        p.P(dw < 150, 0.20, $"hover-dwell-short({F(dw, 0)}ms)");
        p.P(dw > 1500, 0.05, $"hover-dwell-long({F(dw, 0)}ms)");
        p.P(exc > 3, 0.15, $"hover-drift-large({F(exc, 1)}px)");
        p.P(after >= 3 && exc == 0, 0.15, "hover-frozen");
        return Done("hover", p.Score, p.Flags, f);
    }

    public static ActionScore ScoreAmbient(IReadOnlyList<MotionSample> s)
    {
        var p = new Pen();
        if (s.Count < 2) { p.P(true, 0.5, "ambient-too-short"); return Done("ambient", p.Score, p.Flags, new Dictionary<string, object?> { ["n"] = s.Count }); }
        var f = ExtractFeatures(s);
        double dur = D(f, "duration_ms"), ja = D(f, "jitter_autocorr");
        int n = (int)f["n"]!;
        p.P(dur < 200, 0.20, $"ambient-too-short({F(dur, 0)}ms)");
        p.P((bool)f["dt_uniform"]!, 0.15, "uniform-dt-cadence");
        p.P(n >= 8 && ja < 0.1, 0.10, $"white-noise-jitter({F(ja, 2)})");
        p.P(n >= 4 && D(f, "straightness") > 0.9995, 0.15, "perfectly-straight-path");
        return Done("ambient", p.Score, p.Flags, f);
    }

    /// <summary>Pointer kinds: glide | move | click | pointer | drag | ambient | hover.</summary>
    public static ActionScore ScoreAction(string kind, IReadOnlyList<MotionSample> samples)
    {
        switch ((kind ?? "").ToLowerInvariant())
        {
            case "glide": case "move": case "click": case "pointer":
                var m = ScoreMotion(samples);
                return Done("glide", m.Score, m.Flags.ToList(), m.Features);
            case "drag": return ScoreDrag(samples);
            case "ambient": return ScoreAmbient(samples);
            case "hover": return ScoreHover(samples);
            default: throw new ArgumentException($"not a pointer action kind: {kind}", nameof(kind));
        }
    }

    /// <summary>typed | type | keys.</summary>
    public static ActionScore ScoreAction(string kind, IReadOnlyList<KeySample> samples) =>
        (kind ?? "").ToLowerInvariant() is "typed" or "type" or "keys" ? ScoreTyped(samples) : throw new ArgumentException($"not a key action kind: {kind}", nameof(kind));

    /// <summary>wheel.</summary>
    public static ActionScore ScoreAction(string kind, IReadOnlyList<WheelSample> samples) =>
        (kind ?? "").ToLowerInvariant() == "wheel" ? ScoreWheel(samples) : throw new ArgumentException($"not a wheel action kind: {kind}", nameof(kind));

    /// <summary>Any kind, samples as a JSON array in the Python/Node shape ({x,y,t,buttons} / {key,down,up} /
    /// {t,dy,dx,mode}): the form the shared vectors and the score files use.</summary>
    public static ActionScore ScoreActionJson(string kind, JsonElement samples)
    {
        static double G(JsonElement e, string k, double d = 0) =>
            e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
        var k = (kind ?? "").ToLowerInvariant();
        var items = samples.EnumerateArray().Where(e => e.ValueKind == JsonValueKind.Object).ToList();
        if (k is "typed" or "type" or "keys")
            return ScoreTyped(items.Select(e => new KeySample(e.TryGetProperty("key", out var kk) ? kk.GetString() ?? "" : "", G(e, "down"), G(e, "up", G(e, "down")))).ToList());
        if (k == "wheel")
            return ScoreWheel(items.Select(e => new WheelSample(G(e, "t"), G(e, "dy"), G(e, "dx"), (int)G(e, "mode"))).ToList());
        return ScoreAction(k, items.Select(e => new MotionSample(G(e, "x"), G(e, "y"), G(e, "t"), (int)G(e, "buttons"))).ToList());
    }
}
