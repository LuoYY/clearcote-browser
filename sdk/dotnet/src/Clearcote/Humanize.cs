using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// Options for the humanized click family (<c>HumanClickAsync</c>, <c>HumanDblClickAsync</c>,
/// <c>HumanHoverAsync</c>), with the meaning Playwright gives its own click options. <c>ILocator.HumanClickAsync</c>
/// keeps its <see cref="LocatorClickOptions"/> parameter (which honours the same options); this record converts
/// to it implicitly. Every one is honoured:
/// the button, the click count and the hold shape the press, the modifiers are held around it, the
/// position replaces the persona's point, <see cref="Force"/> skips the checks and <see cref="Trial"/>
/// performs no action at all.
/// </summary>
public sealed record HumanClickOptions
{
    /// <summary>The mouse button (default left). A right click opens the context menu, a middle click fires auxclick.</summary>
    public MouseButton? Button { get; init; }

    /// <summary>Presses in a row: 2 is a double click, two presses 70-170 ms apart. Default 1 (2 for
    /// <c>HumanDblClickAsync</c>).</summary>
    public int? ClickCount { get; init; }

    /// <summary>How long each press is held, in ms. Default: the persona's click hold.</summary>
    public float? Delay { get; init; }

    /// <summary>Keys held around the whole press: down 80-360 ms before it (the modifier's own 40-110 ms lead plus
    /// the 40-250 ms pre-press pause before any press), up 20-80 ms after it.
    /// <see cref="KeyboardModifier.ControlOrMeta"/> is passed through for Playwright to resolve to the key
    /// the engine's platform uses.</summary>
    public IEnumerable<KeyboardModifier>? Modifiers { get; init; }

    /// <summary>The point to press, relative to the top-left corner of the element's PADDING box (as in
    /// Playwright). Default: a point drawn from the persona, never the exact centre.</summary>
    public Position? Position { get; init; }

    /// <summary>Skip the enabled check and the covered-point check.</summary>
    public bool Force { get; init; }

    /// <summary>Perform NO action: run Playwright's own trial (actionability checks only) and return.</summary>
    public bool Trial { get; init; }

    /// <summary>How long to wait for the element, in ms. Default: the page's default timeout. A timeout
    /// throws Playwright's timeout exception; it never falls back to a second native wait.</summary>
    public float? Timeout { get; init; }

    /// <summary>The same options as Playwright's <see cref="LocatorClickOptions"/> (NoWaitAfter has no
    /// humanized meaning and is dropped).</summary>
    public static HumanClickOptions From(LocatorClickOptions? options) => options is null ? new HumanClickOptions() : new HumanClickOptions
    {
        Button = options.Button, ClickCount = options.ClickCount, Delay = options.Delay, Modifiers = options.Modifiers,
        Position = options.Position, Force = options.Force == true, Trial = options.Trial == true, Timeout = options.Timeout,
    };

    /// <summary>The same options as Playwright's <see cref="LocatorClickOptions"/>, so a
    /// <see cref="HumanClickOptions"/> can be passed to <c>ILocator.HumanClickAsync</c> (and to Playwright's
    /// own ClickAsync).</summary>
    public static implicit operator LocatorClickOptions(HumanClickOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        return options.ToClick();
    }

    internal LocatorClickOptions ToClick() => new()
    {
        Button = Button, ClickCount = ClickCount, Delay = Delay, Modifiers = Modifiers, Position = Position,
        Force = Force ? true : null, Trial = Trial ? true : null, Timeout = Timeout,
    };

    internal LocatorDblClickOptions ToDblClick() => new()
    {
        Button = Button, Delay = Delay, Modifiers = Modifiers, Position = Position,
        Force = Force ? true : null, Trial = Trial ? true : null, Timeout = Timeout,
    };

    internal LocatorHoverOptions ToHover() => new()
    {
        Modifiers = Modifiers, Position = Position, Force = Force ? true : null, Trial = Trial ? true : null, Timeout = Timeout,
    };

    internal NormalizedClick Normalize(int? defaultCount = null) => Forms.NormalizeClickOptions(
        Button?.ToString().ToLowerInvariant(), ClickCount ?? defaultCount, Delay,
        Modifiers?.Select(m => m.ToString()), Position is { } p ? (p.X, p.Y) : null, Force, Trial);
}

/// <summary>
/// Human input for Playwright .NET: pointer paths, click/key dwell, landing dispersion and
/// engine-fired dropdown selection.
///
/// WHY THIS IS EXTENSION METHODS AND NOT A TRANSPARENT PATCH, unlike Python and Node. Those two
/// SDKs replace <c>page.click</c> / <c>Locator.press</c> on the live object, so an existing script
/// becomes humanised with one launch flag and no edits. C# cannot do that: <see cref="IPage"/> and
/// <see cref="ILocator"/> are interfaces implemented by internal Playwright classes, there is no
/// prototype to reassign, and a decorating wrapper would have to reimplement the entire interface
/// and would still be bypassed the moment a caller obtained a page from an event or a popup.
///
/// So the .NET surface is explicit: <c>HumanClickAsync</c> beside <c>ClickAsync</c>. The cost is
/// that it is opt-in per call rather than per launch. The benefit is that it is honest — there is
/// no launch flag that silently does nothing, which is what a half-working transparent patch would
/// have been.
///
/// WHAT EACH METHOD DEFEATS, all measured against clearcotelabs.com/audit:
///   HumanClickAsync       pointer-press-dwell-duration (a scripted click holds ~0.3ms; no switch
///                         can), pointer-landing-dispersion (a driver lands on the identical
///                         sub-pixel point every time), pointer-movement-precedes-click
///   HumanTypeAsync /      key-press-dwell-duration (a scripted keystroke holds ~1-3ms). Both also
///   HumanPressAsync       place the cursor once per page before the first keystroke: keys that
///                         arrive in a session carrying no pointer events at all are separable on
///                         that alone, however good the dwell is.
///   HumanSelectOptionAsync  interaction-select-change-trust — Playwright's SelectOptionAsync
///                         dispatches input+change from script, so both arrive isTrusted=false and
///                         the engine cannot produce that. Opening the drop-down with a click and
///                         committing with Enter makes the BROWSER fire them, trusted. The IPage overload exists
///                         because page.SelectOptionAsync(selector, value) is what most scripts
///                         actually write, and it goes straight to the untrusted path.
///   HumanWheelAsync /     pointer-movement-precedes-click, wheel edition: a wheel event carries the
///   HumanScrollAsync      cursor's coordinates, so an unpositioned scroll delivers every one of them
///                         at the driver's origin, a corner no reader scrolls from. Also scrolls in
///                         whole notches of the persona platform's size, in bursts — a CDP wheel event
///                         always reports a full notch, so a sub-notch slice is an event no device emits.
///   HumanDragToAsync      pointer-press-dwell-duration at both ends of the gesture (grab hesitation
///                         after the press, settle before the release) plus a held-button path
///                         instead of down/teleport/up, which a range thumb reads as no drag at all.
///   PressAndHoldAsync     one long press (a hold-to-confirm or "press and hold" button) with sparse
///                         whole-pixel drift while held, every move reporting the button down (Hold.cs).
///
/// FORMS (a port of the Python SDK's humanized form input; the planning lives in Forms.cs and is pinned to
/// the other SDKs by sdk/testdata/humanize_forms_vectors.json):
///   HumanFillAsync        classifies the field: text is clicked, cleared only when it holds something
///                         (held Ctrl/Cmd+A then Backspace), typed and verified; a range is dragged or
///                         clicked to its value and nudged with arrows; a date/time/month/week editor is
///                         clicked into and typed segment by segment in the order the browser shows them.
///   HumanSelectOptionAsync  opens a drop-down with a click, moves the highlight by type-ahead and arrows
///                         and commits with Enter: ONE trusted change; list boxes get row clicks
///                         (Ctrl/Cmd-click for more rows) and wheel scrolling.
///   HumanCheckAsync / HumanUncheckAsync / HumanSetCheckedAsync  a humanized click only when the state
///                         must change, verified afterwards.
///   HumanTypeAsync        occasional fat-finger slips, corrected with Backspace -- never in passwords,
///                         e-mail, numbers, phone numbers, URLs or one-time codes, and only what actually
///                         landed in the field is deleted. CLEARCOTE_HUMANIZE_TYPO_RATE (default 0.02,
///                         0 disables) is read at every call.
/// Every DOM read goes through the isolated world (IsolatedWorld) or Playwright's own utility-world
/// queries, never the page's main world.
/// </summary>
public static partial class Humanize
{
    // Persona per page. ConditionalWeakTable so a closed page is collectable — a Dictionary here
    // would pin every page a long-lived process ever opened.
    private static readonly ConditionalWeakTable<IPage, Persona> Personas = new();
    private static readonly ConditionalWeakTable<IPage, PointerState> Pointers = new();

    // Known is the invariant every press depends on: false means the driver's cursor has never been
    // moved on this page and still sits at the document origin. Ambient records that the one-per-page
    // pre-keystroke placement has been spent. NotchPx is the wheel notch of the platform the page
    // claims, read on the first wheel and kept (0 until then).
    private sealed class PointerState
    {
        public double X; public double Y; public bool Known; public bool Ambient; public int NotchPx;
        public long ViewAt = long.MinValue; public (double W, double H)? View;   // ViewBoundsAsync's cache
    }

    /// <summary>The viewport a planned path is kept inside (<see cref="Pacing.KeepInView"/>: the planner's overshoot
    /// arcs left the window near its edges, verifier B), read in the isolated world at most every 2 s; null when it
    /// cannot be read.</summary>
    private static async Task<(double W, double H)?> ViewBoundsAsync(IPage page, PointerState st)
    {
        long now = Environment.TickCount64;
        if (st.ViewAt == long.MinValue || now - st.ViewAt > 2000)
        {
            (double W, double H)? vp = null;
            try
            {
                var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.Viewport).ConfigureAwait(false);
                if (r is { } el && el.ValueKind == JsonValueKind.Array && el.GetArrayLength() == 2
                    && el[0].GetDouble() > 0 && el[1].GetDouble() > 0)
                    vp = (el[0].GetDouble(), el[1].GetDouble());
            }
            catch { /* detached / navigating / no CDP: no clamp */ }
            st.ViewAt = now;
            st.View = vp;
        }
        return st.View;
    }

    // Test seam: a forced rollover share for one page, in place of the persona's (the tests pin 0 and 1).
    internal static readonly ConditionalWeakTable<IPage, StrongBox<double>> RolloverOverride = new();

    // Test seam: a forced typo rate for one page, in place of CLEARCOTE_HUMANIZE_TYPO_RATE (the tests that
    // assert an exact key sequence pin 0; the live typo tests pin a high rate).
    internal static readonly ConditionalWeakTable<IPage, StrongBox<double>> TypoRateOverride = new();

    /// <summary>
    /// The environment variable that sets the per-character fat-finger rate of the humanized typing
    /// (<c>HumanTypeAsync</c>, <c>HumanFillAsync</c>): a number from 0 to 1, read at every call. Typos are ON by
    /// default at 0.02; set it to <c>0</c> to turn them off.
    /// </summary>
    public const string TypoRateEnv = "CLEARCOTE_HUMANIZE_TYPO_RATE";

    /// <summary>
    /// Per-character fat-finger rate: <see cref="TypoRateEnv"/> (0..1; 0 disables typos), read at every
    /// call, else <see cref="Forms.DefaultTypoRate"/>. Same parsing as the Python SDK's typo_rate(): an
    /// unparsable value or NaN is the default.
    /// </summary>
    internal static double TypoRate()
    {
        var v = Environment.GetEnvironmentVariable(TypoRateEnv);
        if (string.IsNullOrEmpty(v)) return Forms.DefaultTypoRate;
        if (!double.TryParse(v.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var f)) return Forms.DefaultTypoRate;
        return double.IsNaN(f) ? Forms.DefaultTypoRate : Math.Min(1.0, Math.Max(0.0, f));
    }

    // ONE humanized action at a time per page. Two concurrent calls on one page (Task.WhenAll of two fills)
    // share the pointer and the keyboard focus: their glides, clicks and keys interleave and each types into
    // the other's field. The gate is not re-entrant, so a public method that calls another public one is
    // marked with Inside (the page whose gate this logical flow already holds) and passes straight through.
    private static readonly ConditionalWeakTable<IPage, SemaphoreSlim> Gates = new();
    private static readonly AsyncLocal<IPage?> Inside = new();

    private static async Task<T> SerialAsync<T>(IPage page, Func<Task<T>> body)
    {
        if (ReferenceEquals(Inside.Value, page)) return await body().ConfigureAwait(false);
        var gate = Gates.GetValue(page, _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync().ConfigureAwait(false);
        Inside.Value = page;   // flows into body(); restored for the caller when this method returns
        try
        {
            return await body().ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }

    private static Task SerialAsync(IPage page, Func<Task> body)
        => SerialAsync<bool>(page, async () => { await body().ConfigureAwait(false); return true; });

    private static double TypoRateFor(IPage page)
    {
        if (TypoRateOverride.TryGetValue(page, out var forced)) return forced.Value;
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable(TypoRateEnv))) return TypoRate();
        return TraitsFor(page).TypoRate;   // the persona's 0.5-4 % (Keys.TypingTraitsOf)
    }

    // ------------------------------------------------------------------ lane D keyboard state
    /// <summary>Per-page keyboard state: when the last typed run ended (the submit / Tab pause it owes) and the
    /// keys the CALLER holds through <see cref="HumanKeyDownAsync"/> (key -> when it went down, the hold it is owed).</summary>
    private sealed class KeyState
    {
        public long? Typed;
        public readonly Dictionary<string, (long At, double Dwell)> Held = new();
    }
    private static readonly ConditionalWeakTable<IPage, KeyState> KeyStates = new();
    private static KeyState KeysOf(IPage page) => KeyStates.GetValue(page, _ => new KeyState());
    private static readonly ConditionalWeakTable<IPage, TypingTraits> Traits = new();
    /// <summary>The page persona's typing identity (Keys.TypingTraitsOf: a salted side stream of its seed).</summary>
    internal static TypingTraits TraitsFor(IPage page) => Traits.GetValue(page, pg => Keys.TypingTraitsOf(PersonaFor(pg).Seed));
    /// <summary>Every keyboard-path sleep goes through here (a test seam: the cadence tests record what is slept).</summary>
    internal static Func<double, Task> KeySleep = ms => Task.Delay((int)Math.Max(0, Math.Round(ms)));
    private static readonly Rng Unseeded = () => Rnd.NextDouble();

    // How long a covered click point is re-checked (an overlay fading out) before the native path takes over.
    internal const float CoverCheckMs = 400;
    // The timeout of the utility-world value reads that prove a fill landed. (It was also the one longer look
    // of the Playwright trial that checked a point inside a child frame; no Playwright trial runs in humanized
    // mode any more: such a point is answered by its <iframe> element, see IsolatedWorld.NodeAtAsync.)
    internal const float CoverRetryMs = 2000;

    // Points tried when resolving a control through the browser's hit test: a floating label or an icon
    // often sits over a field's CENTRE, so the edges get a chance too.
    private static readonly (double Fx, double Fy)[] ProbePoints =
        { (0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2) };

    /// <summary>
    /// Attach a motor persona to a page. Pass the fingerprint seed so the same identity moves the
    /// same way here as it does under the Python and Node SDKs; omit it for a random persona.
    /// </summary>
    public static void Attach(IPage page, object? seed = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        Personas.AddOrUpdate(page, Motion.MakePersona(seed));
    }

    /// <summary>The page's persona, creating a random one on first use if none was attached.</summary>
    public static Persona PersonaFor(IPage page)
    {
        ArgumentNullException.ThrowIfNull(page);
        return Personas.GetValue(page, _ => Motion.MakePersona());
    }

    private static PointerState StateFor(IPage page) => Pointers.GetValue(page, _ => new PointerState());

    private static readonly Random Rnd = Random.Shared;
    private static double Rand(double lo, double hi) => lo + Rnd.NextDouble() * (hi - lo);

    /// <summary>
    /// Give the pointer an origin when it has never had one: start somewhere plausible rather than
    /// teleporting from (0,0), which is itself a shape a real session does not produce.
    /// </summary>
    /// <summary>
    /// The REAL viewport, in CSS px — NOT <c>page.ViewportSize</c>.
    ///
    /// Clearcote.cs sets <c>ViewportSize = ViewportSize.NoViewport</c> on every headed context launch
    /// (and on headless ones where the engine's persona owns the screen), so
    /// that innerWidth tracks the real OS window instead of Playwright's emulated 1280x720 (an
    /// emulated viewport on a headed window is itself a tell). With NoViewport, page.ViewportSize is
    /// null for the life of the page, so a <c>?? 1280 / ?? 800</c> fallback was taken on EVERY headed
    /// run: the scroll anchor's "is the pointer inside the viewport" test then compared against a box
    /// unrelated to the window, and on a maximized display a cursor legitimately at x=1400 read as
    /// out-of-bounds and got re-homed on every scroll — the exact motion that gate exists to avoid.
    /// innerWidth/innerHeight are correct in both modes; the constant is a last resort only.
    /// </summary>
    /// <summary>
    /// Is this Playwright failure a TIMEOUT?
    ///
    /// Playwright for .NET exports exactly ONE exception type — <c>PlaywrightException</c>. There is
    /// no <c>Microsoft.Playwright.TimeoutException</c> (verified by reflecting over
    /// Microsoft.Playwright 1.49.0: PlaywrightException is the only exported Exception subtype), so
    /// a timeout can only be told apart by its message, which Playwright formats as
    /// "Timeout 30000ms exceeded". Catching the wrong thing here matters: a timeout must be
    /// rethrown so a missing element fails on ITS deadline, while any other Playwright error falls
    /// through to the native call.
    /// </summary>
    private static bool IsTimeout(PlaywrightException e) =>
        e.Message.Contains("Timeout", StringComparison.OrdinalIgnoreCase)
        && e.Message.Contains("exceeded", StringComparison.OrdinalIgnoreCase);

    /// <summary>
    /// A timeout of either shape: Playwright .NET raises <see cref="System.TimeoutException"/> for its
    /// TimeoutError, and <see cref="IsTimeout(PlaywrightException)"/> covers a message-only one. The form
    /// routes catch everything else and hand the action to Playwright, so this is what keeps a missing
    /// element failing on ITS deadline instead of waiting it out a second time natively.
    /// </summary>
    private static bool IsTimeoutError(Exception e) =>
        e is System.TimeoutException || (e is PlaywrightException pe && IsTimeout(pe));

    private static async Task<(double W, double H)> ViewportAsync(IPage page)
    {
        try
        {
            // Read in an isolated world (IsolatedWorld): the page never sees it.
            var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.Viewport).ConfigureAwait(false);
            if (r is { } el && el.ValueKind == JsonValueKind.Array && el.GetArrayLength() == 2)
            {
                double w = el[0].GetDouble(), h = el[1].GetDouble();
                if (w > 0 && h > 0) return (w, h);
            }
        }
        catch { /* detached / navigating — fall through to the driver's own answer */ }
        var vp = page.ViewportSize;
        return (vp?.Width ?? 1280, vp?.Height ?? 800);
    }

    private static async Task SeedPointerAsync(IPage page, PointerState st)
    {
        if (st.Known) return;
        var (vw, vh) = await ViewportAsync(page).ConfigureAwait(false);
        st.X = Rand(vw * 0.2, vw * 0.8);
        st.Y = Rand(vh * 0.2, vh * 0.8);
        st.Known = true;
        await page.Mouse.MoveAsync((float)st.X, (float)st.Y).ConfigureAwait(false);
    }

    /// <summary>
    /// Glide the cursor to a point along a planned path, dispatching every sample. <c>settle</c> adds
    /// the seating jiggle a hand makes when it stops on a target — what a range thumb needs at the end
    /// of a drag leg to register the final value.
    /// </summary>
    private static async Task GlideAsync(IPage page, Persona p, PointerState st, double toX, double toY,
        double targetW, bool settle = false)
    {
        KeysOf(page).Typed = null;   // the hand left the keyboard: no submit / Tab pause owed
        await SeedPointerAsync(page, st).ConfigureAwait(false);
        var steps = Motion.PlanMove(new Point(st.X, st.Y), new Point(toX, toY), p, targetW, settle);
        // Native moves carry button state, so a button held via Mouse.DownAsync stays pressed
        // across the whole path — that is what makes a drag a drag rather than a teleport.
        //
        // Paced against a WALL CLOCK (Pacing): each sample goes out at its planned time and late ones
        // are skipped (never the last, and no skip hops more than 40 px, or 20 px once nothing has been
        // dispatched for 40 ms: Pacing.MaxJumpPx / IdleHopPx). "Move, then sleep the planned gap" added
        // the ~27 ms move round trip to every 6-8 ms sample: a 1.05 s planned glide took 3.5-5.4 s on
        // PRO r38 (SC 209).
        var vp = await ViewBoundsAsync(page, st).ConfigureAwait(false);
        await Pacing.WalkAsync(vp is { } v ? Pacing.KeepInView(steps, v.W, v.H) : steps,
            (x, y) => page.Mouse.MoveAsync((float)x, (float)y)).ConfigureAwait(false);
        st.X = toX;
        st.Y = toY;
    }

    /// <summary>
    /// Move the pointer onto the element along a human path, without pressing. False means there was
    /// no box to aim at (or the element was not workable), which is the caller's signal to hand the
    /// whole action to Playwright rather than fail it.
    /// </summary>
    private static async Task<bool> GlideOntoAsync(ILocator locator)
    {
        var page = locator.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        try
        {
            await locator.ScrollIntoViewIfNeededAsync().ConfigureAwait(false);
            var bb = await locator.BoundingBoxAsync().ConfigureAwait(false);
            if (bb is null) return false;
            var box = new Box(bb.X, bb.Y, bb.Width, bb.Height);
            var from = st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
            // Aim at a point drawn from the persona, not the geometric centre — the centre every time is
            // what a landing-dispersion check measures.
            var target = Motion.ClickPoint(box, from, p);
            await GlideAsync(page, p, st, target.X, target.Y, Math.Max(6, bb.Width)).ConfigureAwait(false);
            return true;
        }
        catch (PlaywrightException e) when (IsTimeout(e))
        {
            // An element that never appears has to fail on ITS timeout. Swallowing this would send the
            // caller to a native call that waits the whole timeout a second time before saying so.
            throw;
        }
        catch (PlaywrightException)
        {
            return false;
        }
    }

    /// <summary>
    /// Guarantee a pointer position before a button goes down. Mouse.DownAsync presses wherever the
    /// driver's cursor sits, and on a page nothing has moved on that is the document origin — the
    /// press then lands on &lt;body&gt; and a drag written as down/move/up grabs nothing at all.
    /// </summary>
    private static async Task EnsurePointerAsync(IPage page, Persona p, PointerState st)
    {
        if (st.Known) return;
        await AmbientPlaceAsync(page, p, st).ConfigureAwait(false);
        await SeedPointerAsync(page, st).ConfigureAwait(false);   // no-op once ambient has placed it
    }

    /// <summary>
    /// A brief non-goal cursor movement, spent at most once per page, before the first keystroke on a
    /// page the pointer has never been on. Placement only: it never presses, so focus and the active
    /// element are untouched, and its endpoints keep clear of the focused control so a widget that
    /// focuses on hover cannot pull focus out from under the keys mid-word.
    /// </summary>
    private static async Task AmbientPlaceAsync(IPage page, Persona p, PointerState st)
    {
        if (st.Ambient || st.Known) return;
        st.Ambient = true;   // spent up front: a failure here must not re-run this on every keystroke
        try
        {
            var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
            var avoid = await FocusedRectAsync(page).ConfigureAwait(false);
            int legs = Rnd.NextDouble() < 0.5 ? 2 : 1;
            for (int i = 0; i < legs; i++)
            {
                var to = IdlePoint(w, h, avoid);
                await GlideAsync(page, p, st, to.X, to.Y, 60).ConfigureAwait(false);
                if (i + 1 < legs) await Task.Delay((int)Rand(80, 240)).ConfigureAwait(false);
            }
        }
        catch (PlaywrightException)
        {
            // Best effort: the keystrokes still go out, just without the pointer history.
        }
    }

    /// <summary>The focused element's viewport rect, so ambient placement can steer around it.</summary>
    private static async Task<Box?> FocusedRectAsync(IPage page)
    {
        try
        {
            // Read in an isolated world (IsolatedWorld): the page never sees it.
            var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.FocusedRect).ConfigureAwait(false);
            if (r is { ValueKind: JsonValueKind.Object } rect)
                return new Box(rect.GetProperty("x").GetDouble(), rect.GetProperty("y").GetDouble(),
                               rect.GetProperty("w").GetDouble(), rect.GetProperty("h").GetDouble());
        }
        catch (PlaywrightException)
        {
            // No frame to evaluate in: place without steering rather than skip the placement.
        }
        return null;
    }

    /// <summary>A plausible idle spot, kept 40px clear of <paramref name="avoid"/> where possible.</summary>
    private static Point IdlePoint(double w, double h, Box? avoid)
    {
        Point pt = new(w * 0.5, h * 0.5);
        for (int i = 0; i < 6; i++)
        {
            pt = new Point(Rand(w * 0.12, w * 0.88), Rand(h * 0.12, h * 0.80));
            if (avoid is not Box a) break;
            if (pt.X < a.X - 40 || pt.X > a.X + a.Width + 40 ||
                pt.Y < a.Y - 40 || pt.Y > a.Y + a.Height + 40) break;
        }
        return pt;
    }

    /// <summary>
    /// Move to the element along a human path and press it the way <paramref name="options"/> asks: the
    /// button, the click count (a double click is two presses with a human gap), the hold
    /// (<c>Delay</c>, else the persona's), modifiers held around the press, and a <c>Position</c> measured
    /// from the padding box. <c>Trial</c> performs no action; <c>Force</c> skips the enabled and
    /// covered-point checks. A <see cref="HumanClickOptions"/> converts to this parameter implicitly, so
    /// <c>locator.HumanClickAsync(new HumanClickOptions { Button = MouseButton.Right })</c> works too.
    /// <para>Strict like Playwright's locator actions: more than one match goes to the native click, which
    /// raises Playwright's strict-mode error. A covered point, a disabled element (without Force) or an
    /// element that will not hold still goes to the native click too; a timeout propagates.</para>
    /// </summary>
    public static async Task HumanClickAsync(this ILocator locator, LocatorClickOptions? options = null)
    {
        // One overload on purpose: a second ILocator overload taking HumanClickOptions would make an existing
        // `HumanClickAsync(new() { ... })` call ambiguous. HumanClickOptions converts to this type instead.
        ArgumentNullException.ThrowIfNull(locator);
        var o = HumanClickOptions.From(options);
        await SerialAsync(locator.Page, () => ClickCoreAsync(locator, o, strict: true, () => locator.ClickAsync(options)))
            .ConfigureAwait(false);
    }

    private static async Task ClickCoreAsync(ILocator locator, HumanClickOptions options, bool strict, Func<Task> native)
    {
        if (!await RoutableAsync(locator, strict).ConfigureAwait(false))
        {
            await native().ConfigureAwait(false);
            return;
        }
        await TargetedClickAsync(locator, options, options.Normalize(), noClick: false, native).ConfigureAwait(false);
    }

    /// <summary>The first element matching <paramref name="selector"/>, clicked as
    /// <see cref="HumanClickAsync(ILocator,LocatorClickOptions)"/> does (page actions are not strict).</summary>
    public static async Task HumanClickAsync(this IPage page, string selector, HumanClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        var o = options ?? new HumanClickOptions();
        var loc = page.Locator(selector).First;
        await SerialAsync(page, () => ClickCoreAsync(loc, o, strict: false, () => loc.ClickAsync(o.ToClick())))
            .ConfigureAwait(false);
    }

    /// <summary>
    /// A humanized double click: two presses with the persona's hold and a 70-170 ms gap between them
    /// (Playwright's DblClickAsync presses for ~0 ms). Options as for
    /// <see cref="HumanClickAsync(ILocator,LocatorClickOptions)"/>; ClickCount defaults to 2.
    /// </summary>
    public static async Task HumanDblClickAsync(this ILocator locator, HumanClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SerialAsync(locator.Page, () => DblClickCoreAsync(locator, options ?? new HumanClickOptions(), strict: true))
            .ConfigureAwait(false);
    }

    private static async Task DblClickCoreAsync(ILocator locator, HumanClickOptions options, bool strict)
    {
        Task Native() => locator.DblClickAsync(options.ToDblClick());
        if (!await RoutableAsync(locator, strict).ConfigureAwait(false))
        {
            await Native().ConfigureAwait(false);
            return;
        }
        await TargetedClickAsync(locator, options, options.Normalize(defaultCount: 2), noClick: false, Native).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanDblClickAsync(ILocator,HumanClickOptions)"/>
    public static async Task HumanDblClickAsync(this IPage page, string selector, HumanClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        var loc = page.Locator(selector).First;
        await SerialAsync(page, () => DblClickCoreAsync(loc, options ?? new HumanClickOptions(), strict: false)).ConfigureAwait(false);
    }

    /// <summary>
    /// Move onto the element along a human path without pressing. Modifiers are held across the move,
    /// as Playwright does; Position, Force, Trial and Timeout as for
    /// <see cref="HumanClickAsync(ILocator,LocatorClickOptions)"/>.
    /// </summary>
    public static async Task HumanHoverAsync(this ILocator locator, HumanClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SerialAsync(locator.Page, () => HoverCoreAsync(locator, options ?? new HumanClickOptions(), strict: true))
            .ConfigureAwait(false);
    }

    private static async Task HoverCoreAsync(ILocator locator, HumanClickOptions options, bool strict)
    {
        Task Native() => locator.HoverAsync(options.ToHover());
        if (!await RoutableAsync(locator, strict).ConfigureAwait(false))
        {
            await Native().ConfigureAwait(false);
            return;
        }
        await TargetedClickAsync(locator, options, options.Normalize(), noClick: true, Native).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanHoverAsync(ILocator,HumanClickOptions)"/>
    public static async Task HumanHoverAsync(this IPage page, string selector, HumanClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        var loc = page.Locator(selector).First;
        await SerialAsync(page, () => HoverCoreAsync(loc, options ?? new HumanClickOptions(), strict: false)).ConfigureAwait(false);
    }

    /// <summary>
    /// Locator actions are STRICT in Playwright: more than one match is an error. The humanized routes
    /// act on what they resolve, so they check the count first and let the native call raise its own
    /// strict-mode error when there is more than one.
    /// </summary>
    private static async Task<bool> StrictOkAsync(ILocator locator)
    {
        try
        {
            return await locator.CountAsync().ConfigureAwait(false) <= 1;
        }
        catch (PlaywrightException)
        {
            return false;
        }
    }

    /// <summary>
    /// A locator that enters an iframe (<c>page.FrameLocator(...).Locator(...)</c>): its selector carries
    /// <c>internal:control=enter-frame</c>. The hit test and the isolated world only see the main frame, so
    /// such a locator goes to Playwright's own method (a humanized fill there appended "new" to "old").
    /// </summary>
    internal static bool IsFrameLocator(ILocator locator)
        => locator.ToString()?.Contains("internal:control=enter-frame", StringComparison.Ordinal) == true;

    /// <summary>May the humanized route act on this locator? Not inside a frame, and (for locator actions)
    /// not when it matches more than one element.</summary>
    private static async Task<bool> RoutableAsync(ILocator locator, bool strict)
        => !IsFrameLocator(locator) && (!strict || await StrictOkAsync(locator).ConfigureAwait(false));

    /// <summary>Internal: the humane route cannot apply here; run Playwright's own call instead.</summary>
    private sealed class NativeRouteException : Exception
    {
        public NativeRouteException(string why) : base(why) { }
    }

    /// <summary>
    /// The humanized click / double click / hover (and the click inside check and uncheck). Ported from
    /// the Python SDK's _targeted_click: resolve the element's box in view (BoxOfAsync: the caller's
    /// timeout, no wait with Force), check it is enabled, choose the point (the caller's padding-box position
    /// or the persona's), wait 50 ms and prove with the browser's own hit test that the point still reaches
    /// the element (stability and covered-by in one: nothing dispatched, the pointer does not move), glide
    /// there and press as asked. Anything the route cannot prove goes to <paramref name="native"/>; a timeout
    /// propagates. No Playwright trial runs at any point: <c>Trial</c> is answered by <see cref="TrialAsync"/>.
    /// </summary>
    private static async Task TargetedClickAsync(ILocator loc, HumanClickOptions options, NormalizedClick o, bool noClick,
        Func<Task> native)
    {
        if (o.Trial)
        {
            await TrialAsync(loc, options.Timeout, o, noClick).ConfigureAwait(false);   // performs NO action
            return;
        }
        var page = loc.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        bool goNative = false;
        try
        {
            // Playwright skips actionability with Force: no wait then -- a target it would click hidden goes
            // native at once instead of timing out first.
            var bb = await BoxOfAsync(loc, options.Timeout, o.Force).ConfigureAwait(false);
            if (bb is null) goNative = Fallback("click: no box");
            else if (!o.Force && !await loc.IsEnabledAsync().ConfigureAwait(false)) goNative = Fallback("click: not enabled");
            else
            {
                double x, y;
                if (o.Position is { } pos)
                {
                    (x, y) = await PositionPointAsync(page, bb, pos).ConfigureAwait(false);
                }
                else
                {
                    var from = st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
                    var pt = Motion.ClickPoint(new Box(bb.X, bb.Y, bb.Width, bb.Height), from, p);
                    x = pt.X;
                    y = pt.Y;
                }
                // Whole pixels: the hit test answers for the pixel DOM.getNodeForLocation rounds to, so the press goes
                // to that pixel too. A press at 971.6 next to a badge ending at 972 was proven at 972 and landed on
                // the badge, reported as a success (verifier B, SC 210).
                x = Math.Round(x);
                y = Math.Round(y);
                if (!o.Force)
                {
                    // Stability, then covered-by: 50 ms on, the element must still fill `bb` (an animation in
                    // flight does not) and own the click point -- no trusted press at a point some overlay
                    // owns. Either failing goes native: Playwright waits for it.
                    await Task.Delay(50).ConfigureAwait(false);
                    if (await ReachesPointAsync(page, bb, x, y, CssOf(loc)).ConfigureAwait(false) != true)
                        goNative = Fallback("click: the point does not reach the element (covered, moving or clipped)");
                }
                if (!goNative)
                {
                    if (noClick && o.Modifiers.Count > 0)
                    {
                        // hover with modifiers: Playwright holds them across the move
                        var held = new List<string>();
                        try
                        {
                            foreach (var m in o.Modifiers)
                            {
                                await page.Keyboard.DownAsync(m).ConfigureAwait(false);
                                held.Add(m);
                                await Task.Delay((int)Rand(40, 110)).ConfigureAwait(false);
                            }
                            await GlideAsync(page, p, st, x, y, Math.Max(6, bb.Width)).ConfigureAwait(false);
                        }
                        finally
                        {
                            for (int k = held.Count - 1; k >= 0; k--)
                            {
                                await Task.Delay((int)Rand(20, 80)).ConfigureAwait(false);
                                // one failed keyup must not strand the others
                                try { await page.Keyboard.UpAsync(held[k]).ConfigureAwait(false); }
                                catch (PlaywrightException) { /* best effort */ }
                            }
                        }
                        return;
                    }
                    await GlideAsync(page, p, st, x, y, Math.Max(6, bb.Width)).ConfigureAwait(false);
                    if (!noClick) await PressAtAsync(page, p, o).ConfigureAwait(false);
                    return;
                }
            }
        }
        catch (Exception e) when (!IsTimeoutError(e))
        {
            goNative = Fallback("click: " + e.Message);
        }
        if (goNative) await native().ConfigureAwait(false);
    }

    // Test seam: told why a humanized route handed an action to Playwright (the live tests print it when
    // they catch a route falling back where it should not).
    internal static Action<string>? FallbackTrace;

    private static bool Fallback(string why)
    {
        FallbackTrace?.Invoke(why);
        return true;
    }

    /// <summary>
    /// Does a press at viewport (x, y) reach the element filling <paramref name="box"/>? True / false, or
    /// null when it cannot be told here (CDP is unavailable). A point inside a child frame is answered by the
    /// &lt;iframe&gt; element that holds it (IsolatedWorld.NodeAtAsync).
    /// </summary>
    private static async Task<bool?> HitInBoxAsync(IPage page, LocatorBoundingBoxResult box, double x, double y, string? sel = null)
    {
        var w = IsolatedWorld.For(page);
        if (await w.NodeAtAsync(x, y).ConfigureAwait(false) is not int nid) return null;
        var b = new Dictionary<string, object?>
        {
            ["x"] = (double)box.X, ["y"] = (double)box.Y, ["width"] = (double)box.Width, ["height"] = (double)box.Height,
        };
        // A plain-CSS selector for the element (CssOf): whether the hit is THAT element (IsolatedWorld.HitInBox),
        // not just one that fills the box.
        if (!string.IsNullOrEmpty(sel)) b["sel"] = sel;
        var r = await w.CallOnNodeAsync(nid, IsolatedWorld.HitInBox, b).ConfigureAwait(false);
        return r is not { } v ? null : v.ValueKind == JsonValueKind.True;
    }

    /// <summary>
    /// The covered-point check, through the browser's own hit test read in the isolated world: nothing is
    /// dispatched and the pointer does not move. It replaced a Playwright TRIAL action, which moves the REAL
    /// pointer onto the element and back and performs a press with its events blocked: verifier B saw, before
    /// every humanized click, a mousemove inside the target, a 450-500 px jump back, then the glide, and a
    /// pointerdown/pointerup/click (left, even for a right click) that window capture listeners could see.
    /// A covered point is looked at again for <see cref="CoverCheckMs"/> (an overlay fading out). A point
    /// inside a child frame is answered by the &lt;iframe&gt; element that holds it, so no Playwright trial runs
    /// for it either. False when covered; null when the hit test cannot run (no CDP): the caller goes native.
    /// </summary>
    private static async Task<bool?> ReachesPointAsync(IPage page, LocatorBoundingBoxResult box, double x, double y, string? sel = null)
    {
        var deadline = Environment.TickCount64 + (long)CoverCheckMs;
        var hit = await HitInBoxAsync(page, box, x, y, sel).ConfigureAwait(false);
        while (hit == false && Environment.TickCount64 < deadline)
        {
            await Task.Delay(80).ConfigureAwait(false);
            hit = await HitInBoxAsync(page, box, x, y, sel).ConfigureAwait(false);
        }
        if (hit is null) FallbackTrace?.Invoke("click: the hit test cannot run here");
        return hit;
    }

    /// The viewport point of Playwright's <c>Position</c> (relative to the PADDING box: the border widths are added).
    private static async Task<(double X, double Y)> PositionPointAsync(IPage page, LocatorBoundingBoxResult bb, (double X, double Y) pos)
    {
        var info = await DescribeAsync(page, bb, null).ConfigureAwait(false);
        return (bb.X + (info?.Bl ?? 0) + pos.X, bb.Y + (info?.Bt ?? 0) + pos.Y);
    }

    /// <summary>
    /// The page's default action timeout (SetDefaultTimeout, else the context's, else Playwright's 30 s). Playwright
    /// .NET has no public getter: read through its TimeoutSettings, 30 s when that shape is not there.
    /// </summary>
    internal static float DefaultTimeoutMs(IPage page)
    {
        try
        {
            const System.Reflection.BindingFlags F = System.Reflection.BindingFlags.Instance
                | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public;
            for (var t = page.GetType(); t is not null; t = t.BaseType)
            {
                if (t.GetField("_timeoutSettings", F)?.GetValue(page) is not { } ts) continue;
                foreach (var m in ts.GetType().GetMethods(F))
                    if (m.Name == "Timeout" && m.GetParameters().Length == 1
                        && m.Invoke(ts, new object?[] { null }) is float f && float.IsFinite(f))
                        return f;
            }
        }
        catch (Exception)
        {
            // not this Playwright's shape
        }
        return 30_000;
    }

    /// <summary>
    /// <c>Trial = true</c>, answered by the SDK: Playwright's actionability checks -- visible, enabled (not for
    /// hover), stable, and the click point receives the press (the browser's own hit test, read in the isolated
    /// world) -- retried until the caller's timeout (or the page default), then a TimeoutException (what Playwright .NET throws on a timeout).
    /// NO pointer move and NO press: Playwright's own trial moves the real pointer onto the target and dispatches
    /// pointerdown/pointerup/click that page listeners registered before its blocker see (SC 209: 6/6). The
    /// point checked is Playwright's: Position, else the centre. Scrolling the element into view is part of the
    /// checks, as in Playwright. With Force only the element must exist.
    /// </summary>
    private static async Task TrialAsync(ILocator loc, float? timeout, NormalizedClick o, bool noClick)
    {
        var page = loc.Page;
        float ms = timeout ?? DefaultTimeoutMs(page);
        long? end = ms > 0 ? Environment.TickCount64 + (long)ms : null;
        float Left() => end is long e ? Math.Max(1, e - Environment.TickCount64) : 0;
        if (o.Force)
        {
            await loc.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Attached, Timeout = Left() })
                .ConfigureAwait(false);
            return;
        }
        while (true)
        {
            string why = "element is not visible";
            var bb = await BoxOfAsync(loc, Left(), force: false).ConfigureAwait(false);   // waits for visible; throws Playwright's timeout
            if (bb is not null)
            {
                if (!noClick && !await loc.IsEnabledAsync().ConfigureAwait(false))
                {
                    why = "element is not enabled";
                }
                else
                {
                    var (x, y) = o.Position is { } pos
                        ? await PositionPointAsync(page, bb, pos).ConfigureAwait(false)
                        : (bb.X + bb.Width / 2.0, bb.Y + bb.Height / 2.0);
                    await Task.Delay(50).ConfigureAwait(false);   // stable: 50 ms on, the element still fills `bb`
                    if (await HitInBoxAsync(page, bb, x, y, CssOf(loc)).ConfigureAwait(false) != false) return;   // null: cannot tell -- nothing to add
                    why = $"element is not stable, or another element would receive the pointer at ({x:0}, {y:0})";
                }
            }
            if (end is long done && Environment.TickCount64 >= done)
                throw new System.TimeoutException(
                    $"Timeout {ms:0}ms exceeded.\nCall log:\n  - waiting for {loc}\n  - {why}");
            await Task.Delay(end is long e2 ? (int)Math.Clamp(e2 - Environment.TickCount64, 0, 100) : 100).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Press where the last glide ended, the way the options ask: the button, the click count (a double
    /// click is two presses with a human gap, not one event), the caller's hold or the persona's, and
    /// modifiers held around the whole press with a lead before it and a lag after, as fingers do.
    /// </summary>
    private static async Task PressAtAsync(IPage page, Persona p, NormalizedClick o)
    {
        var held = new List<string>();
        try
        {
            foreach (var m in o.Modifiers)
            {
                await page.Keyboard.DownAsync(m).ConfigureAwait(false);
                held.Add(m);
                await Task.Delay((int)Rand(40, 110)).ConfigureAwait(false);
            }
            await PrePressAsync(p).ConfigureAwait(false);   // the hand confirms the target before it presses
            var button = o.Button switch { "right" => MouseButton.Right, "middle" => MouseButton.Middle, _ => MouseButton.Left };
            for (int i = 1; i <= o.ClickCount; i++)
            {
                // Delay <= 0 or non-finite was normalised to null: the persona's hold (Task.Delay(-1) is forever).
                double hold = o.Delay is double d && d > 0 && double.IsFinite(d) ? d : Motion.ClickHold(p);
                var sent = System.Diagnostics.Stopwatch.StartNew();
                await page.Mouse.DownAsync(new MouseDownOptions { Button = button, ClickCount = i }).ConfigureAwait(false);
                try
                {
                    await Task.Delay(HoldRest(hold, sent)).ConfigureAwait(false);
                }
                finally
                {
                    // never leave the button down: every later move would be a drag
                    await page.Mouse.UpAsync(new MouseUpOptions { Button = button, ClickCount = i }).ConfigureAwait(false);
                }
                if (i < o.ClickCount) await Task.Delay((int)Rand(70, 170)).ConfigureAwait(false);   // the gap inside a double click
            }
        }
        finally
        {
            for (int k = held.Count - 1; k >= 0; k--)
            {
                await Task.Delay((int)Rand(20, 80)).ConfigureAwait(false);
                try { await page.Keyboard.UpAsync(held[k]).ConfigureAwait(false); }
                catch (PlaywrightException) { /* best effort */ }
            }
        }
    }

    /// <summary>
    /// A plain left click where the last glide left the pointer (x, y), with the persona's hold, after the
    /// pre-press pause. Down/up, so nothing moves: Mouse.ClickAsync(x, y) sends one more mouseMoved first,
    /// and THAT was the last pointermove the page saw, ~4 ms before the press (the typing focus click,
    /// measured on r38). (x, y) only document the point the glide ended at.
    /// </summary>
    private static async Task ClickAtAsync(IPage page, Persona p, double x, double y)
    {
        _ = x; _ = y;
        await PrePressAsync(p).ConfigureAwait(false);
        double hold = Motion.ClickHold(p);
        var sent = System.Diagnostics.Stopwatch.StartNew();
        await page.Mouse.DownAsync().ConfigureAwait(false);
        try
        {
            await Task.Delay(HoldRest(hold, sent)).ConfigureAwait(false);
        }
        finally
        {
            await page.Mouse.UpAsync().ConfigureAwait(false);   // never leave the button down
        }
    }

    /// <summary>The pause between the pointer coming to rest and the button going down
    /// (<see cref="Forms.PrePressMs"/>: 40-250 ms, right-skewed, centred per identity).</summary>
    private static Task PrePressAsync(Persona p) => Task.Delay((int)Math.Round(Forms.PrePressMs(p.Seed)));

    /// <summary>What is left of a button hold once Mouse.DownAsync has returned. The hold runs from when the
    /// press was SENT: DownAsync returns only after the page has handled the press, and a &lt;select&gt; opens
    /// its popup inside that handling, so a hold slept afterwards reached the page ~30-80 ms longer than
    /// planned (206 ms median for the open click in the SC 209 lab, planned 60-150 ms).</summary>
    private static int HoldRest(double holdMs, System.Diagnostics.Stopwatch sent)
        => (int)Math.Max(0, Math.Min(int.MaxValue, Math.Round(holdMs - sent.Elapsed.TotalMilliseconds)));

    /// <summary>One key (or chord) pressed <paramref name="n"/> times, each HELD for the persona's dwell.</summary>
    private static async Task KeyAsync(IPage page, Persona p, string key, int n = 1, double gapLo = 60, double gapHi = 150)
    {
        for (int j = 0; j < n; j++)
        {
            await page.Keyboard.PressAsync(key, new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
            if (j < n - 1) await Task.Delay((int)Rand(gapLo, gapHi)).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Press on the element and drag to a page point, holding the button for the whole path.
    ///
    /// The press is the part that has to be guaranteed: Mouse.DownAsync presses wherever the driver's
    /// cursor sits, so a drag written as down/move/up on a fresh page grabs the document origin and
    /// the slider never moves. The glide leaves a known position and the press is guarded by it.
    ///
    /// Every leg is a native mouse move, which carries button state, so the button stays down across
    /// the trajectory — a range thumb ignores a move that reports no button held. The grab hesitation
    /// after pressing and the settle before releasing are the two endpoint dwells a scripted drag has
    /// neither of, and the settle jiggle on the final leg is what seats the thumb on a value.
    ///
    /// <c>targetW</c> is the width of whatever is being dropped on, and only scales the Fitts duration.
    /// </summary>
    public static async Task HumanDragToAsync(this ILocator source, double toX, double toY, double targetW = 24)
    {
        ArgumentNullException.ThrowIfNull(source);
        await SerialAsync(source.Page, () => DragToCoreAsync(source, toX, toY, targetW)).ConfigureAwait(false);
    }

    private static async Task DragToCoreAsync(ILocator source, double toX, double toY, double targetW)
    {
        var page = source.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);

        if (!await GlideOntoAsync(source).ConfigureAwait(false))
        {
            // Nothing to aim at: run Playwright's own drag recipe, whose HoverAsync raises the proper
            // actionability error if the element cannot be used at all.
            await source.HoverAsync().ConfigureAwait(false);
            await page.Mouse.DownAsync().ConfigureAwait(false);
            await page.Mouse.MoveAsync((float)toX, (float)toY).ConfigureAwait(false);
            await page.Mouse.UpAsync().ConfigureAwait(false);
            st.X = toX; st.Y = toY; st.Known = true;
            return;
        }

        var (grabMs, releaseMs) = Motion.DragDwell(p);
        await EnsurePointerAsync(page, p, st).ConfigureAwait(false);
        await PrePressAsync(p).ConfigureAwait(false);   // the hand arrives, then the button goes down
        await page.Mouse.DownAsync().ConfigureAwait(false);
        try
        {
            await Task.Delay((int)Math.Round(grabMs)).ConfigureAwait(false);        // grab hesitation
            await GlideAsync(page, p, st, toX, toY, targetW, settle: true).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(releaseMs)).ConfigureAwait(false);     // settle before letting go
        }
        finally
        {
            // A button left down survives into every later action, so the release is unconditional.
            try { await page.Mouse.UpAsync().ConfigureAwait(false); } catch (PlaywrightException) { }
        }
    }

    /// <inheritdoc cref="HumanDragToAsync(ILocator,double,double,double)"/>
    public static async Task HumanDragToAsync(this ILocator source, ILocator target)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(target);
        await SerialAsync(source.Page, () => DragToLocatorCoreAsync(source, target)).ConfigureAwait(false);
    }

    private static async Task DragToLocatorCoreAsync(ILocator source, ILocator target)
    {
        var page = source.Page;
        var st = StateFor(page);

        Point? drop = null;
        double dropW = 24;
        try
        {
            await target.ScrollIntoViewIfNeededAsync().ConfigureAwait(false);
            var tb = await target.BoundingBoxAsync().ConfigureAwait(false);
            if (tb is not null)
            {
                var from = st.Known ? new Point(st.X, st.Y) : new Point(tb.X - 120, tb.Y - 90);
                // Disperse the drop point too: releasing on the target's exact centre every time is
                // the same signature as clicking it.
                drop = Motion.ClickPoint(new Box(tb.X, tb.Y, tb.Width, tb.Height), from, PersonaFor(page));
                dropW = Math.Max(6, tb.Width);
            }
        }
        catch (PlaywrightException e) when (IsTimeout(e))
        {
            throw;   // as in GlideOntoAsync: do not make a missing target wait out two timeouts
        }
        catch (PlaywrightException)
        {
            // fall through to native
        }

        if (drop is not Point d) { await source.DragToAsync(target).ConfigureAwait(false); return; }
        await DragToCoreAsync(source, d.X, d.Y, dropW).ConfigureAwait(false);
    }

    /// <summary>
    /// Scroll with the wheel from a place a reader would actually scroll from, in whole notches.
    ///
    /// A wheel event carries the cursor's coordinates, and Playwright's cursor starts at the document
    /// origin: an unpositioned scroll delivers every wheel at (0,0), a corner where the element under
    /// the pointer is never the content being read. The pointer is only re-homed when it is nowhere
    /// sensible — a human does not move the mouse back to the middle between two scrolls of one page.
    ///
    /// Humanize scrolls WHOLE NOTCHES: the distance delivered is the nearest number of notches (at least
    /// one) of the size the page's platform scrolls per notch — 100px Windows, 120px Linux, 40px macOS —
    /// so a 39px request scrolls 100px under a Windows persona. A wheel event sent over CDP reports a
    /// full notch (wheelDelta ±120) whatever its distance, so the eased sub-notch slices this used to
    /// send (one scroll of 100 as 39/28/20/10/3, measured on r32 and r33) were events no device emits.
    /// Use <c>page.Mouse.WheelAsync</c> where an exact pixel distance matters more than that.
    /// </summary>
    public static async Task HumanWheelAsync(this IPage page, float deltaX, float deltaY)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, () => WheelCoreAsync(page, deltaX, deltaY)).ConfigureAwait(false);
    }

    private static async Task WheelCoreAsync(IPage page, float deltaX, float deltaY)
    {
        var p = PersonaFor(page);
        var st = StateFor(page);
        int notch = await NotchPxAsync(page, st).ConfigureAwait(false);

        try
        {
            var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
            bool homeless = !st.Known || st.X < 2 || st.Y < 2 || st.X > w - 2 || st.Y > h - 2;
            if (homeless)
            {
                // Upper-middle, gaussian, never the exact centre: landing on (w/2, h/2) to the pixel
                // is the same dispersion tell as a driver that clicks computed centres.
                double rx = Math.Clamp(w * 0.5 + Gauss(0, w * 0.12), w * 0.12, w * 0.88);
                double ry = Math.Clamp(h * 0.38 + Gauss(0, h * 0.10), h * 0.12, h * 0.75);
                await GlideAsync(page, p, st, rx, ry, 80).ConfigureAwait(false);
                await Task.Delay((int)Rand(50, 190)).ConfigureAwait(false);   // read a moment before the flick
            }
        }
        catch (PlaywrightException)
        {
            // Placement is best effort; the scroll below still runs.
        }

        // One notch per event and one axis per event, like a real wheel or tilt: vertical, then horizontal.
        var plan = new List<(float X, float Y)>();
        for (int i = Motion.WheelNotches(deltaY, notch); i > 0; i--) plan.Add((0, Math.Sign(deltaY) * notch));
        for (int i = Motion.WheelNotches(deltaX, notch); i > 0; i--) plan.Add((Math.Sign(deltaX) * notch, 0));
        int next = 0;
        try
        {
            // A finger spins a wheel in bursts: a few notches close together, then a gap while it resets.
            int burstLeft = Rnd.Next(2, 6);
            while (next < plan.Count)
            {
                await page.Mouse.WheelAsync(plan[next].X, plan[next].Y).ConfigureAwait(false);
                if (++next == plan.Count) break;
                // Local sleep, never WaitForTimeoutAsync: that is a CDP round-trip per call, so it emits
                // protocol traffic a detector can score — a self-inflicted tell inside the humanize path.
                if (--burstLeft == 0)
                {
                    await Task.Delay((int)Rand(110, 320)).ConfigureAwait(false);
                    burstLeft = Rnd.Next(2, 6);
                }
                else await Task.Delay((int)Rand(18, 55)).ConfigureAwait(false);
                if (Rnd.NextDouble() < 0.07)
                    await Task.Delay((int)Rand(150, 450)).ConfigureAwait(false);   // mid-scroll reading pause
                // A hand resting on the mouse does not hold it still through a long scroll, and a run of
                // wheel events on byte-identical coordinates is not what a hand produces.
                if (plan.Count >= 6 && Rnd.NextDouble() < 0.06) await DriftAsync(page, st).ConfigureAwait(false);
            }
        }
        catch (PlaywrightException)
        {
            // Deliver the rest plainly: humanize must never lose scroll the caller asked for. Whole
            // notches and no sleeps — a sub-notch remainder would be the very event this method avoids.
            for (; next < plan.Count; next++)
                await page.Mouse.WheelAsync(plan[next].X, plan[next].Y).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Pixels per wheel notch on this page: read once, from the platform the page claims (the
    /// persona's, which need not be the host's), then kept. The host OS stands in when the read fails.
    /// </summary>
    private static async Task<int> NotchPxAsync(IPage page, PointerState st)
    {
        if (st.NotchPx > 0) return st.NotchPx;
        // Read in an isolated world (IsolatedWorld): the page never sees it.
        var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.Platform).ConfigureAwait(false);
        string? platform = r is { ValueKind: JsonValueKind.String } s ? Motion.PlatformFromNavigator(s.GetString()) : null;
        platform ??= OperatingSystem.IsWindows() ? "windows" : OperatingSystem.IsMacOS() ? "macos" : "linux";
        return st.NotchPx = Motion.WheelNotchPx[platform];
    }

    /// <summary>Vertical <see cref="HumanWheelAsync(IPage,float,float)"/>: the common reading scroll.</summary>
    public static async Task HumanScrollAsync(this IPage page, float deltaY)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, () => WheelCoreAsync(page, 0, deltaY)).ConfigureAwait(false);
    }

    /// <summary>A few pixels of hand drift, kept inside the viewport so the wheel stays over the content.</summary>
    private static async Task DriftAsync(IPage page, PointerState st)
    {
        if (!st.Known) return;
        var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
        double nx = Math.Clamp(st.X + Gauss(0, 2.2), 2, w - 2);
        double ny = Math.Clamp(st.Y + Gauss(0, 2.2), 2, h - 2);
        await page.Mouse.MoveAsync((float)nx, (float)ny).ConfigureAwait(false);
        st.X = nx; st.Y = ny;
    }

    /// <summary>
    /// Type into the element key-by-key with human dwell and cadence (and the occasional corrected slip;
    /// see <see cref="HumanTypeAsync(IPage,string)"/>). A visible text field is clicked into first unless it
    /// already has focus (a click into a field that holds text moves the caret to wherever it lands).
    /// Anything else -- a hidden helper textarea of a rich editor, a button, a locator inside a frame, a
    /// field an overlay keeps the click from reaching -- is typed by Playwright's own
    /// <c>PressSequentiallyAsync</c>, which focuses it without a click. Strict: more than one match raises
    /// Playwright's strict-mode error.
    /// </summary>
    public static async Task HumanTypeAsync(this ILocator locator, string text, float? delay = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(text);
        var page = locator.Page;
        await SerialAsync(page, async () =>
        {
            if (await RoutableAsync(locator, strict: true).ConfigureAwait(false)
                && await ClickIntoAsync(locator, null).ConfigureAwait(false))
            {
                var p = PersonaFor(page);
                await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);   // no-op once a click placed it
                await TypeTextAsync(page, p, text, null, typos: true, floorMs: delay ?? 0, exact: false).ConfigureAwait(false);
                return;
            }
            await locator.PressSequentiallyAsync(text, delay is null ? null : new LocatorPressSequentiallyOptions { Delay = delay }).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// Does <paramref name="loc"/> hold the keyboard focus? Asked through Playwright's utility-world selector
    /// engine (<c>:focus</c>), so it works for every element and every locator and the page sees nothing.
    /// (The hit test only knew form controls, so a focused BUTTON read as unfocused.)
    /// </summary>
    private static async Task<bool> FocusedAsync(ILocator loc)
    {
        try
        {
            return await loc.And(loc.Page.Locator(":focus")).CountAsync().ConfigureAwait(false) > 0;
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>(box, info) when <paramref name="loc"/> is a visible text-entry control a person would click
    /// into before typing (the hit test proves the element), else null. A button, a link, a hidden helper
    /// textarea or a slider is not: Playwright focuses those without a click.</summary>
    private static async Task<(LocatorBoundingBoxResult Box, FieldInfo Info)?> TextEntryAsync(ILocator loc, float? timeout)
    {
        if (!await loc.IsVisibleAsync().ConfigureAwait(false)) return null;
        var box = await BoxOfAsync(loc, timeout, force: false).ConfigureAwait(false);
        var info = await DescribeAsync(loc.Page, box, IsolatedWorld.FormControl).ConfigureAwait(false);
        return box is not null && info is not null && Forms.ClassifyField(info.Tag, info.Type, info.Ce) == "text"
            ? (box, info) : null;
    }

    /// <summary>
    /// Make <paramref name="loc"/> the focus the way a person does -- a click -- unless it already is. True
    /// when it holds focus afterwards. False (the caller uses Playwright's own call) for anything that is not
    /// a visible text-entry control, and when the click did not put the caret there (an overlay took it).
    /// A timeout propagates.
    /// </summary>
    private static async Task<bool> ClickIntoAsync(ILocator loc, float? timeout)
    {
        try
        {
            if (await FocusedAsync(loc).ConfigureAwait(false)) return true;
            var te = await TextEntryAsync(loc, timeout).ConfigureAwait(false);
            if (te is null) return false;
            var (box, info) = te.Value;
            // lane D: from the field before it, Tab (persona share, verified) -- only into an empty field, as
            // typing appends at the caret and Tab selects a field's text
            if (await TabIntoAsync(loc, info, needEmpty: true).ConfigureAwait(false)) return true;
            if (!await FocusClickAsync(loc.Page, box, ProvenPoint(box, info), loc).ConfigureAwait(false)
                || !await FocusedAsync(loc).ConfigureAwait(false))
                return !Fallback("type: the click did not put the caret in the field");
            await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);
            return true;
        }
        catch (Exception e) when (!IsTimeoutError(e))
        {
            return !Fallback("type: " + e.Message);
        }
    }

    /// <summary>
    /// Type into whatever holds focus, key-by-key with human dwell and cadence. A bare type with no
    /// pointer history on the page gets one ambient placement first: keystrokes from a session where
    /// the mouse never existed are separable from a human's however good the per-key timing is.
    /// Some pairs of plain keys roll over — the next key goes down before the previous one is up — at
    /// the persona's stable <see cref="Motion.RolloverRate"/>.
    /// <para>Occasional fat-finger slips (a neighbouring key, a doubled key or a swapped pair), each
    /// corrected with Backspace, at <see cref="TypoRateEnv"/> (CLEARCOTE_HUMANIZE_TYPO_RATE) per eligible
    /// character: default 0.02, <c>0</c> turns typos off, read at every call. Never in a field where a slip is
    /// a failure rather than a slip: password, e-mail, number, tel and URL inputs, numeric/decimal/tel/email/url
    /// inputmodes and one-time-code, credit-card and password autocomplete tokens -- followed into open shadow
    /// roots, and never when focus is inside a frame the check cannot read. Only what actually LANDED is deleted
    /// (the field's value is read before and after each slip), so an input mask or a maxlength that
    /// swallows the wrong key never costs a real character.</para>
    /// </summary>
    public static async Task HumanTypeAsync(this IPage page, string text, float? delay = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(text);
        await SerialAsync(page, async () =>
        {
            var p = PersonaFor(page);
            await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);
            await TypeTextAsync(page, p, text, null, typos: true, floorMs: delay ?? 0, exact: false).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// The typing core (the Python SDK's _human_type_text): <paramref name="field"/> describes the field
    /// the keys go to for the typo gate (null = read the focused element in the isolated world);
    /// <paramref name="typos"/> false types without slips (the clean retry after a failed check).
    /// </summary>
    private static async Task TypeTextAsync(IPage page, Persona p, string text, FieldTraits? field, bool typos,
        double floorMs = 0, bool exact = true)
    {
        // One key per code point (a surrogate pair is one character, not two half keys).
        var chars = Forms.Chars(text);
        var tr = TraitsFor(page);
        var ks = KeysOf(page);
        double rollover = RolloverOverride.TryGetValue(page, out var forced) ? forced.Value : Motion.RolloverRate(p);
        double rate = typos ? TypoRateFor(page) : 0.0;
        var fld = rate > 0 ? field ?? await ActiveFieldAsync(page).ConfigureAwait(false) : null;
        bool allow = rate > 0 && Forms.TyposAllowed(fld);
        // lane D: a search field may keep a slip (never on fill: exact); the caller's delay is the shortest gap;
        // no second Shift under a Shift the caller holds (HumanKeyDownAsync)
        bool search = allow && !exact && string.Equals(fld?.Type, "search", StringComparison.OrdinalIgnoreCase);
        double floor = Math.Max(0, floorMs);
        bool extShift = ks.Held.ContainsKey("Shift");
        // Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a
        // moment before the key (35-120 ms) and comes up just after it (10-70 ms), held across a run of
        // them. PressAsync("A") alone sent the key with shiftKey=false and no Shift key at all
        // (measured on r28 with "Ab!") -- input no keyboard produces.
        var shift = false;
        try
        {
            for (int i = 0; i < chars.Length; i++)
            {
                var ch = chars[i];
                bool need = NeedsShift(ch) && !extShift;
                if (need && !shift)
                {
                    await page.Keyboard.DownAsync("Shift").ConfigureAwait(false);
                    shift = true;
                    await Task.Delay((int)Rand(35, 120)).ConfigureAwait(false);
                }
                bool typo = false, consumed = false;
                if (allow && !need && !shift && Forms.TypoEligible(ch))
                {
                    var tp = Forms.PlanTypo(chars, i, Rnd.NextDouble, rate);
                    if (tp is not null)
                    {
                        typo = true;
                        bool leave = search && tp.Kind == "sub" && Rnd.NextDouble() < Keys.SearchSlipRate;
                        try
                        {
                            consumed = await DoTypoAsync(page, p, chars, i, tp, leave).ConfigureAwait(false);
                        }
                        catch (PlaywrightException)
                        {
                            // a slip that could not be made is no slip: type the character as planned
                        }
                    }
                }
                // Rollover: one press per character never overlaps two keys, and a real typist's fast
                // pairs do (0 overlapping keydowns in 32 keys, measured on r32/r33). Only two different
                // plain keys, never under Shift, never next to a slip.
                if (!typo && !shift && floor <= 0 && i + 1 < chars.Length && CanRoll(ch) && CanRoll(chars[i + 1]) && ch != chars[i + 1]
                    && Rnd.NextDouble() < rollover && await RollPairAsync(page, ch, chars[i + 1], p).ConfigureAwait(false))
                    ch = chars[++i];   // both typed: carry on as if after the second
                else if (!consumed)
                    await PressOrInsertAsync(page, ch, p).ConfigureAwait(false);
                if (shift && (i == chars.Length - 1 || !NeedsShift(chars[i + 1])))
                {
                    await Task.Delay((int)Rand(10, 70)).ConfigureAwait(false);
                    await page.Keyboard.UpAsync("Shift").ConfigureAwait(false);
                    shift = false;
                }
                if (i < chars.Length - 1)
                {
                    // lane D: the bigram flight model (Keys.PlanGap, the planner Python and Node use): the persona's
                    // WPM, scaled by how the hand travels between the two keys, log-normal noise, word-initial and
                    // punctuation pauses, the whitespace and thinking pauses; the caller's delay as a floor.
                    await KeySleep(Math.Max(floor, Keys.PlanGap(tr, ch, chars[i + 1], Unseeded))).ConfigureAwait(false);
                }
            }
        }
        finally
        {
            // never leave Shift held for the caller's next keystroke
            if (shift)
            {
                try { await page.Keyboard.UpAsync("Shift").ConfigureAwait(false); }
                catch (PlaywrightException) { /* best-effort */ }
            }
            if (chars.Length > 0) ks.Typed = Environment.TickCount64;   // a following Enter / Tab waits (AfterTypingAsync)
        }
    }

    // The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
    // types with Shift held: capitals and the shifted symbols.
    internal static bool NeedsShift(string ch)
        => ch.Length == 1 && ((ch[0] >= 'A' && ch[0] <= 'Z') || "~!@#$%^&*()_+{}|:\"<>?".IndexOf(ch[0]) >= 0);

    // Keys that may roll: lower-case, unshifted, one physical key each on the US layout.
    private const string RollKeys = "abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=";
    private static bool CanRoll(string ch) => ch.Length == 1 && RollKeys.IndexOf(ch[0]) >= 0;

    // Two keys with rollover: b goes down while a is still held, then a comes up, then b. DownAsync
    // inserts the character just as PressAsync does, so the typed value is unchanged. False (nothing
    // sent) when the dwell is too short to overlap inside. A failure releases both keys, best effort,
    // and stops the typing the way a failed press does.
    private static async Task<bool> RollPairAsync(IPage page, string a, string b, Persona p)
    {
        double d1 = Motion.KeyDwell(p), d2 = Motion.KeyDwell(p);
        if (d1 - 8 < 12) return false;
        double overlap = Rand(12, Math.Min(55, d1 - 8));
        try
        {
            await page.Keyboard.DownAsync(a).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(d1 - overlap)).ConfigureAwait(false);
            await page.Keyboard.DownAsync(b).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(overlap)).ConfigureAwait(false);
            await page.Keyboard.UpAsync(a).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(Math.Max(5, d2 - overlap))).ConfigureAwait(false);
            await page.Keyboard.UpAsync(b).ConfigureAwait(false);
            return true;
        }
        catch
        {
            // As with Shift: never leave a key held for the caller's next keystroke.
            foreach (var k in new[] { a, b })
            {
                try { await page.Keyboard.UpAsync(k).ConfigureAwait(false); }
                catch (Exception) { /* best-effort */ }
            }
            throw;
        }
    }

    // One key with the persona's hold. Playwright maps only its US layout; for anything else ("ö",
    // emoji) PressAsync throws "Unknown key", and the text is inserted instead -- the same fallback
    // the Python and Node SDKs use.
    private static async Task PressOrInsertAsync(IPage page, string ch, Persona p)
    {
        try
        {
            await page.Keyboard.PressAsync(ch,
                new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
        }
        catch (PlaywrightException)
        {
            await page.Keyboard.TypeAsync(ch).ConfigureAwait(false);
        }
    }

    /// <summary>The focused field's current value, read in the isolated world (null if unknown).</summary>
    private static async Task<string?> ActiveValueAsync(IPage page)
    {
        var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.ActiveValue).ConfigureAwait(false);
        return r is { ValueKind: JsonValueKind.String } s ? s.GetString() : null;
    }

    /// <summary>What the typo gate needs about the focused field, read in the isolated world (null if
    /// nothing has focus or the read is impossible).</summary>
    private static async Task<FieldTraits?> ActiveFieldAsync(IPage page)
    {
        var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.ActiveField).ConfigureAwait(false);
        if (r is not { ValueKind: JsonValueKind.Object } f) return null;
        bool frame = f.TryGetProperty("frame", out var fr) && fr.ValueKind == JsonValueKind.True;
        return new FieldTraits(JStr(f, "type"), JStr(f, "inputmode"), JStr(f, "autocomplete"), frame);
    }

    /// <summary>Bring the focused field back to <paramref name="target"/> (what it held before a slip) from wherever
    /// a word delete left it: Backspace to the common prefix, then type the rest. Nothing when unreadable.</summary>
    private static async Task RestoreAsync(IPage page, Persona p, string target)
    {
        var got = await ActiveValueAsync(page).ConfigureAwait(false);
        if (got is null || got == target) return;
        var g = Forms.Chars(got);
        var t = Forms.Chars(target);
        int k = 0;
        while (k < Math.Min(g.Length, t.Length) && g[k] == t[k]) k++;
        if (g.Length - k > 64 || t.Length - k > 64) return;
        if (g.Length > k)
        {
            await BackspacesAsync(page, p, g.Length - k).ConfigureAwait(false);
            await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
        }
        string? prev = null;
        foreach (var c in t.Skip(k))
        {
            if (prev is not null) await KeySleep(Keys.FlightMs(TraitsFor(page), prev, c, Unseeded)).ConfigureAwait(false);
            await PressOrInsertAsync(page, c, p).ConfigureAwait(false);
            prev = c;
        }
    }

    private static async Task BackspacesAsync(IPage page, Persona p, int n)
        => await KeyAsync(page, p, "Backspace", n, 70, 150).ConfigureAwait(false);

    /// <summary>How many characters arrived since <paramref name="before"/> (null = cannot tell).</summary>
    private static int? Landed(string? before, string? now)
        => before is null || now is null ? null : now.EnumerateRunes().Count() - before.EnumerateRunes().Count();

    /// <summary>
    /// Make the planned slip at <paramref name="text"/>[i] and correct it. True when it already typed
    /// text[i] itself (the doubled-key case), so the caller must not type it again.
    /// <para>Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
    /// and a Backspace pressed for a key that never arrived deletes a real character. Measured before and
    /// after the slip, in the isolated world -- typos are ~2 % of keys, so this costs nearly nothing.</para>
    /// </summary>
    private static async Task<bool> DoTypoAsync(IPage page, Persona p, string[] text, int i, TypoPlan tp, bool leave = false)
    {
        var before = await ActiveValueAsync(page).ConfigureAwait(false);
        switch (tp.Kind)
        {
            case "sub":
            {
                await PressOrInsertAsync(page, tp.Wrong ?? text[i], p).ConfigureAwait(false);
                var got = Landed(before, await ActiveValueAsync(page).ConfigureAwait(false));
                if (got is int g0 && g0 <= 0) return false;   // swallowed: nothing to correct
                if (leave) return true;                        // a search field: the slip stays in place of text[i]
                int typed = 1;
                string prevKey = tp.Wrong ?? text[i];
                foreach (var c in text.Skip(i + 1).Take(tp.Late))   // late notice: a key or two more
                {
                    await KeySleep(Keys.FlightMs(TraitsFor(page), prevKey, c, Unseeded)).ConfigureAwait(false);
                    await PressOrInsertAsync(page, c, p).ConfigureAwait(false);
                    prevKey = c;
                    typed++;
                }
                await Task.Delay((int)Rand(140, 340)).ConfigureAwait(false);   // noticing
                // lane D: a slip inside a word is fixed with the word-delete chord and the word typed again in
                // Keys.WordFixRate of the cases, when the field can be read to put it back exactly
                int ws = 0;
                for (int j = i - 1; j >= 0; j--) if (text[j] is " " or "\n") { ws = j + 1; break; }
                if (before is not null && i - ws + typed >= 2 && Rnd.NextDouble() < Keys.WordFixRate)
                {
                    await PressKeyAsync(page, p, (OperatingSystem.IsMacOS() ? "Alt" : "Control") + "+Backspace").ConfigureAwait(false);
                    await Task.Delay((int)Rand(120, 260)).ConfigureAwait(false);
                    await RestoreAsync(page, p, before).ConfigureAwait(false);
                    await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
                    return false;
                }
                got = Landed(before, await ActiveValueAsync(page).ConfigureAwait(false));
                await BackspacesAsync(page, p, got is int g ? Math.Max(0, Math.Min(typed, g)) : typed).ConfigureAwait(false);
                await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
                return false;
            }
            case "double":
            {
                await PressOrInsertAsync(page, text[i], p).ConfigureAwait(false);
                await Task.Delay((int)Rand(45, 95)).ConfigureAwait(false);      // the bounce comes fast
                await PressOrInsertAsync(page, text[i], p).ConfigureAwait(false);
                var got = Landed(before, await ActiveValueAsync(page).ConfigureAwait(false));
                await Task.Delay((int)Rand(140, 340)).ConfigureAwait(false);
                int extra = got is int g ? Math.Max(0, g - 1) : 1;
                if (extra > 0)
                {
                    await BackspacesAsync(page, p, extra).ConfigureAwait(false);
                    await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
                }
                return true;
            }
            case "swap":
            {
                await PressOrInsertAsync(page, text[i + 1], p).ConfigureAwait(false);
                await KeySleep(Keys.FlightMs(TraitsFor(page), text[i + 1], text[i], Unseeded)).ConfigureAwait(false);
                await PressOrInsertAsync(page, text[i], p).ConfigureAwait(false);
                var got = Landed(before, await ActiveValueAsync(page).ConfigureAwait(false));
                await Task.Delay((int)Rand(140, 340)).ConfigureAwait(false);
                await BackspacesAsync(page, p, got is int g ? Math.Max(0, Math.Min(2, g)) : 2).ConfigureAwait(false);
                await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
                return false;
            }
        }
        return false;
    }

    private static string? JStr(JsonElement e, string name)
        => e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static double Gauss(double mean, double sd)
    {
        double u = 1.0 - Rnd.NextDouble(), v = Rnd.NextDouble();
        return mean + sd * Math.Sqrt(-2 * Math.Log(u)) * Math.Cos(2 * Math.PI * v);
    }

    /// <summary>
    /// Press a key with the persona's hold (or <paramref name="delay"/>, the caller's own hold). A chord
    /// ("Control+a", "ControlOrMeta+Shift+ArrowLeft") is split: each modifier goes down in order, 40-110 ms before
    /// the next, the key is held, 20-80 ms later the modifiers come up in reverse order -- as one press the modifier
    /// and the key went down within ~1 ms. A modifier held through <see cref="HumanKeyDownAsync"/> is not pressed
    /// again. <paramref name="repeat"/> &gt; 1 holds the key the way an OS auto-repeats it: one keydown, the repeat
    /// delay (250-500 ms), then a keydown with repeat=true every 25-40 ms, <paramref name="repeat"/> keydowns in
    /// all, one keyup. Enter right after typing waits the persona's submit pause (250-900 ms), Tab its Tab pause
    /// (120-400 ms).
    /// </summary>
    public static async Task HumanPressAsync(this IPage page, string key, int repeat = 1, float? delay = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, () => PressFocusedAsync(page, key, repeat, delay)).ConfigureAwait(false);
    }

    private static async Task PressFocusedAsync(IPage page, string key, int repeat = 1, float? delay = null)
    {
        var p = PersonaFor(page);
        // Same reason as the page-level type: one ambient placement when nothing has moved the
        // pointer here yet, so the key does not arrive on a mouse-free session.
        await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);
        await AfterTypingAsync(page, key).ConfigureAwait(false);
        await PressKeyAsync(page, p, key, delay is > 0 ? delay : null, repeat).ConfigureAwait(false);
    }

    /// <summary>The pause between the last typed character and Enter or Tab, less what the caller already spent;
    /// once per typed run (a glide in between cancels it).</summary>
    private static async Task AfterTypingAsync(IPage page, string key)
    {
        var ks = KeysOf(page);
        var t = ks.Typed;
        ks.Typed = null;
        if (t is null) return;
        var (mods, k) = Keys.SplitChord(key);
        double ms;
        if (k == "Enter" && mods.Length == 0) ms = Keys.SubmitPauseMs(TraitsFor(page), Unseeded);
        else if (k == "Tab" && mods.All(m => m == "Shift")) ms = Keys.TabPauseMs(TraitsFor(page), Unseeded);
        else return;
        double rest = ms - (Environment.TickCount64 - t.Value);
        if (rest > 0) await KeySleep(rest).ConfigureAwait(false);
    }

    /// <summary>One key, held (see <see cref="HumanPressAsync(IPage,string,int,float?)"/>).</summary>
    private static async Task HoldKeyAsync(IPage page, Persona p, string k, float? delay, int repeat)
    {
        int n = Math.Max(1, repeat);
        if (n == 1)
        {
            await page.Keyboard.PressAsync(k, new KeyboardPressOptions { Delay = delay ?? (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
            return;
        }
        var rp = Keys.PlanRepeat(Unseeded);
        await page.Keyboard.DownAsync(k).ConfigureAwait(false);
        try
        {
            await KeySleep(rp.FirstMs).ConfigureAwait(false);
            for (int j = 0; j < n - 1; j++)
            {
                if (j > 0) await KeySleep(rp.GapMs).ConfigureAwait(false);
                await page.Keyboard.DownAsync(k).ConfigureAwait(false);   // already down: an autoRepeat keydown
            }
            await KeySleep(rp.ReleaseMs).ConfigureAwait(false);
        }
        finally
        {
            await page.Keyboard.UpAsync(k).ConfigureAwait(false);   // never leave the key held
        }
    }

    private static async Task PressKeyAsync(IPage page, Persona p, string key, float? delay = null, int repeat = 1)
    {
        var (all, k) = Keys.SplitChord(key);
        var held = KeysOf(page).Held;
        var mods = all.Where(m => !held.ContainsKey(m)).ToList();
        if (mods.Count == 0)
        {
            await HoldKeyAsync(page, p, k, delay, repeat).ConfigureAwait(false);
            return;
        }
        var down = new List<string>();
        try
        {
            foreach (var m in mods)
            {
                await page.Keyboard.DownAsync(m).ConfigureAwait(false);
                down.Add(m);
                await KeySleep(Keys.ModifierLeadMs(Unseeded)).ConfigureAwait(false);
            }
            await HoldKeyAsync(page, p, k, delay, repeat).ConfigureAwait(false);
            await KeySleep(Keys.ModifierLagMs(Unseeded)).ConfigureAwait(false);
        }
        finally
        {
            down.Reverse();
            for (int j = 0; j < down.Count; j++)
            {
                if (j > 0) await KeySleep(Rand(10, 40)).ConfigureAwait(false);
                try { await page.Keyboard.UpAsync(down[j]).ConfigureAwait(false); }
                catch (PlaywrightException) { /* one failed keyup must not strand the others */ }
            }
        }
    }

    /// <summary>
    /// Keyboard.DownAsync, tracked: a chord does not press a modifier held this way again, typing types no
    /// second Shift under it, and <see cref="HumanKeyUpAsync"/> gives the key at least a persona hold.
    /// </summary>
    public static async Task HumanKeyDownAsync(this IPage page, string key)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, async () =>
        {
            var held = KeysOf(page).Held;
            if (!held.ContainsKey(key)) held[key] = (Environment.TickCount64, Motion.KeyDwell(PersonaFor(page)));
            await page.Keyboard.DownAsync(key).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanKeyDownAsync"/>
    public static async Task HumanKeyUpAsync(this IPage page, string key)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, async () =>
        {
            var held = KeysOf(page).Held;
            if (held.Remove(key, out var t))
            {
                double rest = t.Dwell - (Environment.TickCount64 - t.At);
                if (rest > 0) await KeySleep(rest).ConfigureAwait(false);
            }
            await page.Keyboard.UpAsync(key).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>Where <paramref name="info"/> sits in the page's Tab order relative to the focused element
    /// (Keys.TabDistance, read in the isolated world), or null.</summary>
    private sealed record TabInfo(int? Dist, bool FromText, bool Focused, bool Activatable);

    private static async Task<TabInfo?> TabDistanceAsync(IPage page, FieldInfo? info)
    {
        if (info is null || info.Rect.Length != 4) return null;
        try
        {
            var r = await IsolatedWorld.For(page).CallOnNodeAsync(info.Node, Keys.TabDistance, new Dictionary<string, object?>
            {
                ["x"] = info.Rect[0], ["y"] = info.Rect[1], ["width"] = info.Rect[2], ["height"] = info.Rect[3],
            }).ConfigureAwait(false);
            if (r is not { ValueKind: JsonValueKind.Object } o) return null;
            int? dist = o.TryGetProperty("dist", out var d) && d.ValueKind == JsonValueKind.Number ? d.GetInt32() : null;
            static bool B(JsonElement o, string n) => o.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.True;
            return new TabInfo(dist, B(o, "fromText"), B(o, "focused"), B(o, "activatable"));
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>
    /// Tab into <paramref name="info"/> instead of clicking it, the way a person moves between adjacent fields:
    /// from a TEXT field 1-3 stops before it (or 1 after: Shift+Tab) in the persona's share (60-85 %);
    /// <paramref name="force"/>: from any focused element, 3 stops either way, no roll (a button a key goes to);
    /// <paramref name="needEmpty"/>: only an empty field (Tab selects its text). True once focus is verified there.
    /// </summary>
    private static async Task<bool> TabIntoAsync(ILocator loc, FieldInfo? info, bool needEmpty = false, bool force = false)
    {
        var page = loc.Page;
        if (needEmpty && !string.IsNullOrEmpty(info?.Value)) return false;
        var td = await TabDistanceAsync(page, info).ConfigureAwait(false);
        if (td is null || td.Dist is not int d || td.Focused) return false;
        if (force ? Math.Abs(d) is < 1 or > 3
                  : !td.FromText || !(d is >= 1 and <= 3 || d == -1) || Rnd.NextDouble() >= TraitsFor(page).TabPref)
            return false;
        var p = PersonaFor(page);
        string key = d > 0 ? "Tab" : "Shift+Tab";
        for (int j = 0; j < Math.Abs(d); j++)
        {
            if (j == 0) await AfterTypingAsync(page, key).ConfigureAwait(false);
            else await KeySleep(Rand(110, 260)).ConfigureAwait(false);
            await PressKeyAsync(page, p, key).ConfigureAwait(false);
        }
        await KeySleep(Rand(40, 120)).ConfigureAwait(false);
        return await FocusedAsync(loc).ConfigureAwait(false);
    }

    /// <summary>
    /// Give <paramref name="loc"/> -- any focusable element -- the focus the way a person does: Tab within 3 stops
    /// (always for something a click would ACTIVATE: a button, a link, a checkbox), else a humanized click for what a
    /// click only focuses. True when focus is verified there; false for an activatable target out of Tab reach (the
    /// pointer has glided onto it; the caller gives the focus with Playwright's FocusAsync, reported).
    /// </summary>
    private static async Task<bool> FocusTargetAsync(ILocator loc, float? timeout)
    {
        var page = loc.Page;
        if (await FocusedAsync(loc).ConfigureAwait(false)) return true;
        var box = await BoxOfAsync(loc, timeout, force: false).ConfigureAwait(false);
        var info = box is null ? null : await DescribeAsync(page, box, Keys.Focusable).ConfigureAwait(false);
        if (box is null || info is null) throw new NativeRouteException("focus: the element could not be proven");
        var td = await TabDistanceAsync(page, info).ConfigureAwait(false);
        bool act = td is null || td.Activatable;   // unknown: never risk an activating click
        if (td?.Dist is int d && Math.Abs(d) is >= 1 and <= 3 && (act || Rnd.NextDouble() < TraitsFor(page).TabPref)
            && await TabIntoAsync(loc, info, force: true).ConfigureAwait(false))
            return true;
        if (!act && !info.Disabled)
        {
            if (await FocusClickAsync(page, box, ProvenPoint(box, info), loc).ConfigureAwait(false)
                && await FocusedAsync(loc).ConfigureAwait(false))
            {
                await KeySleep(Rand(40, 120)).ConfigureAwait(false);
                return true;
            }
            throw new NativeRouteException("focus: the click did not focus it");
        }
        var p = PersonaFor(page);
        var st = StateFor(page);
        var from = st.Known ? new Point(st.X, st.Y) : new Point(box.X - 120, box.Y - 90);
        var pt = Motion.ClickPoint(new Box(box.X, box.Y, box.Width, box.Height), from, p);
        await GlideAsync(page, p, st, Math.Round(pt.X), Math.Round(pt.Y), Math.Max(6, box.Width)).ConfigureAwait(false);
        return false;
    }

    /// <summary>
    /// Press a key on the element. The element gets the focus the way a person gives it: a text field by Tab from
    /// the field before it (when empty) or a click; a button, a link or another focusable element by Tab when it is
    /// within 3 stops, else (not activatable) a click; an activatable element out of Tab reach gets the pointer
    /// over it and Playwright's focus (reported). Never a click on a button (it activated it twice) and never
    /// Playwright's 0 ms focus-and-press. Then the key as <see cref="HumanPressAsync(IPage,string,int,float?)"/>.
    /// </summary>
    public static async Task HumanPressAsync(this ILocator locator, string key, int repeat = 1, float? delay = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        await SerialAsync(page, async () =>
        {
            bool done = false;
            if (await RoutableAsync(locator, strict: true).ConfigureAwait(false))
            {
                try
                {
                    var te = await TextEntryAsync(locator, null).ConfigureAwait(false);
                    done = te is { } t
                        ? await TabIntoAsync(locator, t.Info, needEmpty: true).ConfigureAwait(false)
                          || await ClickIntoAsync(locator, null).ConfigureAwait(false)
                        : await FocusTargetAsync(locator, null).ConfigureAwait(false);
                }
                catch (Exception e) when (!IsTimeoutError(e))
                {
                    done = false;
                }
            }
            if (!done)
            {
                Fallback("press: Playwright's focus (an activatable target out of Tab reach, or not provable)");
                await locator.FocusAsync().ConfigureAwait(false);   // strict, frame-aware: Playwright's own
                await KeySleep(Rand(40, 120)).ConfigureAwait(false);
            }
            await PressFocusedAsync(page, key, repeat, delay).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// Focus the element the way a person does (Tab within 3 stops, in the persona's share for a field; a click
    /// on what a click only focuses), verified; Playwright's FocusAsync for an activatable element out of Tab reach
    /// (reported) and for anything the humane route cannot do.
    /// </summary>
    public static async Task HumanFocusAsync(this ILocator locator, float? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SerialAsync(locator.Page, async () =>
        {
            try
            {
                if (await RoutableAsync(locator, strict: true).ConfigureAwait(false)
                    && await FocusTargetAsync(locator, timeout).ConfigureAwait(false))
                    return;
            }
            catch (Exception e) when (!IsTimeoutError(e)) { /* Playwright's below */ }
            Fallback("focus: Playwright's focus");
            await locator.FocusAsync(new LocatorFocusOptions { Timeout = timeout }).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// Leave the focused element the way the persona leaves fields: Tab to the next stop, or a click on a blank
    /// point beside it (Keys.BlankPoint; Tab when there is none), verified; Playwright's BlurAsync otherwise.
    /// </summary>
    public static async Task HumanBlurAsync(this ILocator locator, float? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        await SerialAsync(page, async () =>
        {
            try
            {
                if (await RoutableAsync(locator, strict: true).ConfigureAwait(false) && await FocusedAsync(locator).ConfigureAwait(false))
                {
                    var p = PersonaFor(page);
                    if (TraitsFor(page).BlurStyle == "click")
                    {
                        var box = await BoxOfAsync(locator, timeout, force: false).ConfigureAwait(false);
                        var (vw, vh) = await ViewportAsync(page).ConfigureAwait(false);
                        var r = box is null ? null : await IsolatedWorld.For(page).EvaluateAsync(Keys.BlankPoint, new Dictionary<string, object?>
                        {
                            ["box"] = new Dictionary<string, double> { ["x"] = box.X, ["y"] = box.Y, ["width"] = box.Width, ["height"] = box.Height },
                            ["vw"] = vw, ["vh"] = vh,
                        }).ConfigureAwait(false);
                        if (r is { ValueKind: JsonValueKind.Array } a && a.GetArrayLength() == 2)
                        {
                            double x = Math.Round(a[0].GetDouble() + Gauss(0, 3)), y = Math.Round(a[1].GetDouble() + Gauss(0, 3));
                            await GlideAsync(page, p, StateFor(page), x, y, 60).ConfigureAwait(false);
                            await ClickAtAsync(page, p, x, y).ConfigureAwait(false);
                            await KeySleep(Rand(40, 120)).ConfigureAwait(false);
                            if (!await FocusedAsync(locator).ConfigureAwait(false)) return;
                        }
                    }
                    await AfterTypingAsync(page, "Tab").ConfigureAwait(false);
                    await PressKeyAsync(page, p, "Tab").ConfigureAwait(false);
                    await KeySleep(Rand(40, 120)).ConfigureAwait(false);
                    if (!await FocusedAsync(locator).ConfigureAwait(false)) return;
                }
            }
            catch (Exception e) when (!IsTimeoutError(e)) { /* Playwright's below */ }
            await locator.BlurAsync(new LocatorBlurOptions { Timeout = timeout }).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// Select a text control's text the way the persona does it -- a triple click (an &lt;input&gt;), or End then
    /// Shift+Home (Ctrl+End, Ctrl+Shift+Home in a textarea; Cmd+A on macOS) --, verified in the isolated world, one
    /// select-all retry; Playwright's SelectTextAsync otherwise.
    /// </summary>
    public static async Task HumanSelectTextAsync(this ILocator locator, float? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        await SerialAsync(page, async () =>
        {
            try
            {
                if (await RoutableAsync(locator, strict: true).ConfigureAwait(false)
                    && await SelectTextCoreAsync(locator, timeout).ConfigureAwait(false))
                    return;
            }
            catch (Exception e) when (!IsTimeoutError(e)) { /* Playwright's below */ }
            Fallback("select_text: Playwright's selectText");
            await locator.SelectTextAsync(new LocatorSelectTextOptions { Timeout = timeout }).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    private static async Task<bool> SelectedAllAsync(IPage page)
    {
        var r = await IsolatedWorld.For(page).EvaluateAsync(Keys.SelectionState).ConfigureAwait(false);
        if (r is not { ValueKind: JsonValueKind.Array } a || a.GetArrayLength() != 3) return false;
        double s0 = a[0].GetDouble(), s1 = a[1].GetDouble(), n = a[2].GetDouble();
        return Math.Min(s0, s1) == 0 && Math.Max(s0, s1) == n;
    }

    private static async Task<bool> SelectTextCoreAsync(ILocator loc, float? timeout)
    {
        var page = loc.Page;
        var p = PersonaFor(page);
        var box = await BoxOfAsync(loc, timeout, force: false).ConfigureAwait(false);
        var info = box is null ? null : await DescribeAsync(page, box, IsolatedWorld.FormControl).ConfigureAwait(false);
        if (box is null || info is null || info.Disabled || Forms.ClassifyField(info.Tag, info.Type, info.Ce) != "text") return false;
        if (TraitsFor(page).SelectStyle == "triple" && info.Tag == "input")
        {
            var st = StateFor(page);
            var from = st.Known ? new Point(st.X, st.Y) : new Point(box.X - 120, box.Y - 90);
            var pt = ProvenPoint(box, info) ?? Motion.ClickPoint(new Box(box.X, box.Y, box.Width, box.Height), from, p);
            await GlideAsync(page, p, st, Math.Round(pt.X), Math.Round(pt.Y), Math.Max(6, box.Width)).ConfigureAwait(false);
            await PressAtAsync(page, p, Forms.NormalizeClickOptions(clickCount: 3)).ConfigureAwait(false);
        }
        else
        {
            if (!await FocusedAsync(loc).ConfigureAwait(false))
            {
                if (!await FocusClickAsync(page, box, ProvenPoint(box, info), loc).ConfigureAwait(false)
                    || !await FocusedAsync(loc).ConfigureAwait(false))
                    return false;
                await KeySleep(Rand(120, 300)).ConfigureAwait(false);
            }
            if (OperatingSystem.IsMacOS()) await PressKeyAsync(page, p, "ControlOrMeta+a").ConfigureAwait(false);
            else if (info.Tag == "input")
            {
                await PressKeyAsync(page, p, "End").ConfigureAwait(false);
                await KeySleep(Rand(80, 200)).ConfigureAwait(false);
                await PressKeyAsync(page, p, "Shift+Home").ConfigureAwait(false);
            }
            else
            {
                await PressKeyAsync(page, p, "ControlOrMeta+End").ConfigureAwait(false);
                await KeySleep(Rand(80, 200)).ConfigureAwait(false);
                await PressKeyAsync(page, p, "ControlOrMeta+Shift+Home").ConfigureAwait(false);
            }
        }
        await KeySleep(Rand(60, 160)).ConfigureAwait(false);
        if (await SelectedAllAsync(page).ConfigureAwait(false)) return true;
        await SelectAllAsync(page, p).ConfigureAwait(false);
        await KeySleep(Rand(60, 160)).ConfigureAwait(false);
        return await SelectedAllAsync(page).ConfigureAwait(false);
    }

    /// <summary>
    /// Choose a &lt;select&gt; option the way a person does, so the ENGINE fires input and change.
    ///
    /// Playwright's SelectOptionAsync assigns the value and dispatches both events from script, so
    /// they arrive with isTrusted=false — and the engine cannot produce an untrusted change, which
    /// makes it one of the most reliable dropdown tells there is.
    ///
    /// <para>A drop-down is opened with a humanized click; while its popup is open, type-ahead letters
    /// and arrows move the highlight without touching the page, and Enter commits: exactly ONE trusted
    /// input and change, however far down the list the option is. A list box (multiple, or size &gt; 1)
    /// gets row clicks -- the first plain, the rest with Ctrl/Cmd held -- with the wheel bringing rows
    /// into view. <paramref name="value"/> matches an option's value OR its label, as in Playwright.
    /// Already selected: nothing fires.</para>
    ///
    /// <para>The result is verified, never assumed: a wrong pick gets one arrow-only correction, then
    /// Playwright's SelectOptionAsync. The opening click is the full humanized click, covered-point check
    /// included, at a point the hit test proved reaches the select (a floating label over its centre cannot
    /// swallow it). Whatever the humane route cannot resolve or do -- a select the hit test cannot prove
    /// (covered, inside a frame), disabled or missing options, element handles -- goes to Playwright's
    /// SelectOptionAsync, which waits and raises as it always does. Strict: more than one match raises
    /// Playwright's strict-mode error.</para>
    /// </summary>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this ILocator locator, string value)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(value);
        return await SelectAsync(locator, new List<SelectWant> { new("value", value, 0) }, null, false, strict: true,
            () => locator.SelectOptionAsync(new[] { value })).ConfigureAwait(false);
    }

    /// <summary>
    /// Choose one or more options, each matched by value OR label (Playwright's string form). A single
    /// select takes the FIRST option, in document order, that matches any of them; a multiple select
    /// gets exactly the matched set. See <see cref="HumanSelectOptionAsync(ILocator,string)"/>.
    /// </summary>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this ILocator locator, IEnumerable<string> values,
        LocatorSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(values);
        var list = values.ToList();
        foreach (var v in list) ArgumentNullException.ThrowIfNull(v, nameof(values));
        return await SelectAsync(locator, list.Select(v => new SelectWant("value", v, 0)).ToList(), options?.Timeout,
            options?.Force == true, strict: true, () => locator.SelectOptionAsync(list, options)).ConfigureAwait(false);
    }

    /// <summary>
    /// Choose an option by <see cref="SelectOptionValue.Index"/>, <see cref="SelectOptionValue.Label"/> or
    /// <see cref="SelectOptionValue.Value"/> (exactly one of them; an option object that combines several
    /// goes to Playwright unchanged). See <see cref="HumanSelectOptionAsync(ILocator,string)"/>.
    /// </summary>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this ILocator locator, SelectOptionValue option,
        LocatorSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(option);
        return await locator.HumanSelectOptionAsync(new[] { option }, options).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,SelectOptionValue,LocatorSelectOptionOptions)"/>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this ILocator locator, IEnumerable<SelectOptionValue> values,
        LocatorSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(values);
        var list = values.ToList();
        return await SelectAsync(locator, WantsOf(list), options?.Timeout, options?.Force == true, strict: true,
            () => locator.SelectOptionAsync(list, options)).ConfigureAwait(false);
    }

    /// <summary>One wanted option: "value" (value or label), "valueOnly" (an option object's Value),
    /// "label" or "index".</summary>
    private readonly record struct SelectWant(string Kind, string? Str, int Index);

    private static List<SelectWant>? WantsOf(IReadOnlyList<SelectOptionValue> list)
    {
        var wants = new List<SelectWant>();
        foreach (var o in list)
        {
            if (o is null) return null;
            int set = (o.Value is not null ? 1 : 0) + (o.Label is not null ? 1 : 0) + (o.Index is not null ? 1 : 0);
            if (set != 1) return null;   // several fields must ALL match in Playwright: native
            if (o.Value is not null) wants.Add(new SelectWant("valueOnly", o.Value, 0));
            else if (o.Label is not null) wants.Add(new SelectWant("label", o.Label, 0));
            else wants.Add(new SelectWant("index", null, o.Index!.Value));
        }
        return wants;
    }

    private static int MatchWant(IReadOnlyList<OptionInfo> opts, SelectWant w)
    {
        switch (w.Kind)
        {
            case "index": return Forms.MatchOption(opts, index: w.Index);
            case "label": return Forms.MatchOption(opts, label: w.Str);
            case "valueOnly":
                for (int i = 0; i < opts.Count; i++) if (opts[i].Value == w.Str) return i;
                return -1;
            default: return Forms.MatchOption(opts, value: w.Str);
        }
    }

    /// <summary>
    /// The select routes: the humane one, else Playwright (which waits and raises as it always does). Once
    /// the humane route has decided, its own errors propagate; a timeout always does.
    /// </summary>
    private static Task<IReadOnlyList<string>> SelectAsync(ILocator loc, List<SelectWant>? wants, float? timeout, bool force,
        bool strict, Func<Task<IReadOnlyList<string>>> native)
        => SerialAsync(loc.Page, async () =>
        {
            if (wants is null || !await RoutableAsync(loc, strict).ConfigureAwait(false))
                return await native().ConfigureAwait(false);
            try
            {
                var res = await SelectHumanizedAsync(loc, wants, timeout, force).ConfigureAwait(false);
                if (res is not null) return res;
                Fallback("select: no humane route, or it could not be shown to have worked");
            }
            catch (Exception e) when (!IsTimeoutError(e))
            {
                Fallback("select: " + e.Message);   // fall through to native
            }
            // outside the try: a native call that raises raises ONCE
            return await native().ConfigureAwait(false);
        });

    /// <summary>
    /// Pick options the way a person does (OpenAndPickAsync / SelectListboxAsync): the selected values as
    /// Playwright returns them, or null when the humane route cannot apply or cannot be shown to have worked.
    /// </summary>
    private static async Task<IReadOnlyList<string>?> SelectHumanizedAsync(ILocator loc, List<SelectWant> wants, float? timeout, bool force)
    {
        if (wants.Count == 0) return null;
        var page = loc.Page;
        var box = await BoxOfAsync(loc, timeout, force).ConfigureAwait(false);
        var info = await DescribeAsync(page, box, "select").ConfigureAwait(false);
        if (box is null || info is null || info.Tag != "select" || info.Disabled) return null;
        var opts = info.Options;
        var idx = new List<int>();
        foreach (var w in wants)
        {
            int i = MatchWant(opts, w);
            if (i < 0 || opts[i].Disabled) return null;   // not there (yet) or disabled: Playwright waits / reports it
            if (!idx.Contains(i)) idx.Add(i);
        }
        if (info.Multiple || info.Size > 1)
        {
            if (!info.Multiple) idx = new List<int> { idx.Min() };
            return await SelectListboxAsync(loc, info, idx).ConfigureAwait(false);
        }
        int t = idx.Min();   // Playwright: the FIRST option (document order) matching any wanted one
        var want = opts[t].Value ?? "";
        if (info.SelectedIndex == t) return new[] { want };   // already selected; forge nothing
        var labels = opts.Select(o => o.Disabled ? null : (o.Label ?? "")).ToList();
        var plan = Forms.PlanSelect(labels, info.SelectedIndex, t);
        if (plan is null) return null;
        // When the centre is covered (a floating label), open it where the hit test proved the select is,
        // as a Playwright position (padding box).
        Position? at = ProvenPoint(box, info) is { } pt
            ? new Position { X = (float)(pt.X - box.X - info.Bl), Y = (float)(pt.Y - box.Y - info.Bt) }
            : null;
        await OpenAndPickAsync(loc, plan, at).ConfigureAwait(false);
        if (await loc.InputValueAsync().ConfigureAwait(false) == want) return new[] { want };
        // One correction from wherever the highlight landed (arrows only), then give up to native.
        var again = await RedescribeAsync(page, info, "select").ConfigureAwait(false);   // the same node: no new handle
        if (again is null) return null;
        var fix = Forms.PlanSelect(labels, again.SelectedIndex, t, maxPrefix: 0);
        if (fix is null) return null;
        await OpenAndPickAsync(loc, fix, at).ConfigureAwait(false);
        return await loc.InputValueAsync().ConfigureAwait(false) == want ? new[] { want } : null;
    }

    /// <summary>
    /// Open the drop-down with a click, move its highlight with the planned keys, commit with Enter: one
    /// trusted input+change, the sequence a mouse-and-keyboard user produces. While the popup is open its
    /// keys never reach the page (measured on r37: value unchanged until Enter). The opening click is the
    /// full humanized click -- covered-point check included -- at <paramref name="at"/> (a padding-box
    /// position the hit test proved) when the centre is covered, so an overlay cannot swallow it and leave
    /// the letters to change a closed select one per key.
    /// </summary>
    private static async Task OpenAndPickAsync(ILocator loc, SelectPlan plan, Position? at)
    {
        var page = loc.Page;
        var p = PersonaFor(page);
        var o = new HumanClickOptions { Position = at };
        await TargetedClickAsync(loc, o, o.Normalize(), noClick: false, () => loc.ClickAsync(o.ToClick()))
            .ConfigureAwait(false);
        await Task.Delay((int)Rand(220, 480)).ConfigureAwait(false);   // the list opens; the eye finds the option
        foreach (var ch in plan.Keys)
        {
            await page.Keyboard.PressAsync(ch.ToString(), new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) })
                .ConfigureAwait(false);
            await Task.Delay((int)Math.Max(40, Gauss(100, 35))).ConfigureAwait(false);   // well inside type-ahead's 1 s window
        }
        if (plan.Arrows != 0)
        {
            await Task.Delay((int)Rand(120, 300)).ConfigureAwait(false);
            await ArrowRunAsync(page, p, plan.Arrows > 0 ? "ArrowDown" : "ArrowUp", Math.Abs(plan.Arrows)).ConfigureAwait(false);
        }
        await Task.Delay((int)Rand(150, 400)).ConfigureAwait(false);
        await KeyAsync(page, p, "Enter").ConfigureAwait(false);
        await Task.Delay((int)Rand(80, 160)).ConfigureAwait(false);
    }

    /// <summary>
    /// <paramref name="n"/> presses of an arrow key in an OPEN drop-down. A long run (8 or more) is HELD, the
    /// way a person crosses a long list: one press, the keyboard's repeat delay (380-560 ms), then auto-repeat
    /// at ~30 a second -- Playwright sends a key that is already down again as an autoRepeat keydown. Pressed
    /// one by one at a human ~180 ms each, 370 rows took 68 s (verifier B). The open popup takes these keys,
    /// so the page sees none of them, only how long the pick took. The key comes up in a finally.
    /// </summary>
    private static async Task ArrowRunAsync(IPage page, Persona p, string key, int n)
    {
        if (n < 8)
        {
            await KeyAsync(page, p, key, n).ConfigureAwait(false);
            return;
        }
        await HoldKeyAsync(page, p, key, null, n).ConfigureAwait(false);   // OS auto-repeat (Keys.PlanRepeat)
    }

    /// <summary>A list box's scroll state and one row's box (IsolatedWorld.ListboxRow), read from the node the
    /// hit test found.</summary>
    private sealed record ListboxState(double[] Box, double[] Row, double Top, double Bottom, double St, double Max, bool[] Sel);

    private static async Task<ListboxState?> ListboxRowAsync(IPage page, FieldInfo info, int i)
    {
        var r = await IsolatedWorld.For(page).CallOnNodeAsync(info.Node, IsolatedWorld.ListboxRow,
            new Dictionary<string, object?> { ["i"] = i }).ConfigureAwait(false);
        if (r is not { ValueKind: JsonValueKind.Object } o) return null;
        static double[] Arr(JsonElement o, string n) => o.GetProperty(n).EnumerateArray().Select(x => x.GetDouble()).ToArray();
        static double Num(JsonElement o, string n) => o.GetProperty(n).GetDouble();
        try
        {
            return new ListboxState(Arr(o, "box"), Arr(o, "row"), Num(o, "top"), Num(o, "bottom"), Num(o, "st"), Num(o, "max"),
                o.GetProperty("sel").EnumerateArray().Select(x => x.ValueKind == JsonValueKind.True).ToArray());
        }
        catch (Exception e) when (e is KeyNotFoundException or InvalidOperationException or FormatException)
        {
            return null;
        }
    }

    /// <summary>
    /// The list box's state once its scroll has come to rest. A wheel notch scrolls the box SMOOTHLY over a
    /// few frames, and a read taken mid-animation shows the row where it was, not where it is going: the old
    /// loop then wheeled the wrong way and oscillated (verifier B: scrollTop 120 -> 240 -> 323 -> 203 -> 83 ->
    /// 0, twice, with the row in view at 120, then a native, untrusted fallback). Two equal reads 50 ms apart
    /// = at rest.
    /// </summary>
    private static async Task<ListboxState?> SettledRowAsync(IPage page, FieldInfo info, int i)
    {
        var prev = await ListboxRowAsync(page, info, i).ConfigureAwait(false);
        for (int k = 0; k < 30; k++)
        {
            await Task.Delay(50).ConfigureAwait(false);
            var cur = await ListboxRowAsync(page, info, i).ConfigureAwait(false);
            if (cur is null || prev is null) return cur;
            if (Math.Abs(cur.St - prev.St) < 0.5 && Math.Abs(cur.Row[1] - prev.Row[1]) < 0.5) return cur;
            prev = cur;
        }
        return prev;
    }

    /// <summary>The visible vertical span (top, bottom) of the row, or null when too little of it shows. A row
    /// cut off by the box edge is still a target a person clicks when most of it shows.</summary>
    private static (double Top, double Bottom)? RowSpan(ListboxState s)
    {
        double y = s.Row[1], w = s.Row[2], h = s.Row[3];
        if (w <= 0 || h <= 0) return null;
        double top = Math.Max(y, s.Top), bottom = Math.Min(y + h, s.Bottom);
        return bottom - top >= Math.Min(h, Math.Max(6.0, h * 0.6)) ? (top, bottom) : null;
    }

    /// <summary>
    /// Wheel the list box until row <paramref name="i"/> shows: the pointer goes over the box once, then as many
    /// notches as the distance needs -- one at first, to learn how far a notch scrolls this box -- each burst
    /// read only once the scroll has settled. Null when it cannot be brought into view (the wheel does not
    /// move the box, or it is at its end).
    /// </summary>
    private static async Task<(double Top, double Bottom, ListboxState S)?> ScrollRowIntoViewAsync(IPage page, FieldInfo info, int i)
    {
        var p = PersonaFor(page);
        var stt = StateFor(page);
        var s = await SettledRowAsync(page, info, i).ConfigureAwait(false);
        if (s is null) return null;
        var span = RowSpan(s);
        double? per = null;
        bool over = false;
        int notches = 0;
        while (span is null && notches < 60)
        {
            double y = s.Row[1], h = s.Row[3];
            double room = s.Bottom - s.Top;
            double margin = Rand(2, Math.Max(3, Math.Min(12, (room - h) * 0.4)));
            double need = y + h > s.Bottom ? (y + h) - s.Bottom + margin : y - s.Top - margin;
            if ((need > 0 && s.St >= s.Max - 0.5) || (need < 0 && s.St <= 0.5)) return null;
            if (!over)
            {
                double bx = s.Box[0], bw = s.Box[2];
                await GlideAsync(page, p, stt, bx + bw * Rand(0.3, 0.7), s.Top + room * Rand(0.3, 0.7), Math.Max(20, room)).ConfigureAwait(false);
                await Task.Delay((int)Rand(80, 200)).ConfigureAwait(false);
                over = true;
            }
            int n = per is not double pr ? 1
                : Math.Max(1, Math.Min(8, (int)Math.Floor(Math.Abs(need) / pr) + (Math.Abs(need) % pr > pr * 0.25 ? 1 : 0)));
            int notch = await NotchPxAsync(page, stt).ConfigureAwait(false);
            for (int j = 0; j < n; j++)
            {
                await page.Mouse.WheelAsync(0, need > 0 ? notch : -notch).ConfigureAwait(false);
                notches++;
                if (j < n - 1) await Task.Delay((int)Rand(30, 70)).ConfigureAwait(false);
            }
            double before = s.St;
            s = await SettledRowAsync(page, info, i).ConfigureAwait(false);
            if (s is null) return null;
            double moved = Math.Abs(s.St - before);
            if (moved < 0.5) return null;   // the wheel did not scroll the box
            per = moved / n;
            span = RowSpan(s);
        }
        return span is { } sp ? (sp.Top, sp.Bottom, s) : null;
    }

    /// <summary>
    /// A list box (multiple or size &gt; 1): click the rows -- the first plainly (which also drops any
    /// other selection), the rest with Ctrl/Cmd held -- scrolling the box with the wheel to bring a row
    /// into view the way a person does. Null when it cannot be shown to have worked.
    /// </summary>
    private static async Task<IReadOnlyList<string>?> SelectListboxAsync(ILocator loc, FieldInfo info, List<int> idx)
    {
        var page = loc.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        var want = idx.OrderBy(i => i).ToList();
        var opts = info.Options;
        var values = want.Select(i => opts[i].Value ?? "").ToArray();
        var selectedNow = Enumerable.Range(0, opts.Count).Where(i => opts[i].Selected).ToList();
        if (selectedNow.SequenceEqual(want)) return values;
        for (int k = 0; k < want.Count; k++)
        {
            if (await ScrollRowIntoViewAsync(page, info, want[k]).ConfigureAwait(false) is not { } got) return null;
            var (top, bottom, s) = got;
            var r = s.Row;
            double px = Rand(r[0] + r[2] * 0.12, r[0] + Math.Min(r[2] * 0.6, 140));
            double py = (top + bottom) / 2 + Gauss(0, Math.Max(0.5, (bottom - top) * 0.12));
            py = Math.Min(Math.Max(py, top + 1), bottom - 1);
            await GlideAsync(page, p, st, px, py, bottom - top).ConfigureAwait(false);
            if (k > 0)
                // ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
                await PressAtAsync(page, p, Forms.NormalizeClickOptions(modifiers: new[] { "ControlOrMeta" })).ConfigureAwait(false);
            else
                await ClickAtAsync(page, p, px, py).ConfigureAwait(false);
            await Task.Delay((int)Rand(150, 350)).ConfigureAwait(false);
        }
        var final = await ListboxRowAsync(page, info, want[0]).ConfigureAwait(false);
        if (final is null) return null;
        var sel = Enumerable.Range(0, final.Sel.Length).Where(i => final.Sel[i]).ToList();
        return sel.SequenceEqual(want) ? values : null;
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,string)"/>
    /// <remarks>
    /// The selector form exists because page.SelectOptionAsync(selector, value) is the call scripts
    /// actually reach for, and every one of them lands on the untrusted script-assigned change that
    /// interaction-select-change-trust reads. The first match, as Playwright's page method takes it, then
    /// the same route as the locator overload — no second implementation to drift out of step.
    /// </remarks>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IPage page, string selector, string value)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        ArgumentNullException.ThrowIfNull(value);
        return await SelectAsync(page.Locator(selector).First, new List<SelectWant> { new("value", value, 0) }, null, false,
            strict: false, () => page.SelectOptionAsync(selector, new[] { value })).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,IEnumerable{string},LocatorSelectOptionOptions)"/>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IPage page, string selector, IEnumerable<string> values,
        PageSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        ArgumentNullException.ThrowIfNull(values);
        var list = values.ToList();
        foreach (var v in list) ArgumentNullException.ThrowIfNull(v, nameof(values));
        bool strict = options?.Strict == true;
        return await SelectAsync(strict ? page.Locator(selector) : page.Locator(selector).First,
            list.Select(v => new SelectWant("value", v, 0)).ToList(), options?.Timeout, options?.Force == true, strict,
            () => page.SelectOptionAsync(selector, list, options)).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,SelectOptionValue,LocatorSelectOptionOptions)"/>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IPage page, string selector, SelectOptionValue option,
        PageSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(option);
        return await page.HumanSelectOptionAsync(selector, new[] { option }, options).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,SelectOptionValue,LocatorSelectOptionOptions)"/>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IPage page, string selector, IEnumerable<SelectOptionValue> values,
        PageSelectOptionOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        ArgumentNullException.ThrowIfNull(values);
        var list = values.ToList();
        bool strict = options?.Strict == true;
        return await SelectAsync(strict ? page.Locator(selector) : page.Locator(selector).First, WantsOf(list), options?.Timeout,
            options?.Force == true, strict, () => page.SelectOptionAsync(selector, list, options)).ConfigureAwait(false);
    }

    // ------------------------------------------------------------------------------------- fill
    /// <summary>
    /// Fill a form field the way a person does, chosen by what the field IS (Playwright's FillAsync sets
    /// the value from script whatever the field):
    /// <list type="bullet">
    /// <item>text-like (text, search, email, password, tel, url, number, unknown types, textarea,
    /// contenteditable): click into it, clear it only if it holds something (Ctrl/Cmd+A then Backspace,
    /// each held for the persona's dwell; then End and Backspaces if anything is left), type with the
    /// persona's cadence and the occasional corrected slip (see <see cref="HumanTypeAsync(IPage,string)"/>),
    /// then check the value; one clean retry without slips, else Playwright's FillAsync. A maxlength field
    /// gets only what it can hold (the value Playwright's FillAsync leaves there).</item>
    /// <item>range: drag the thumb (or click the track) to the value, then nudge with held arrow keys
    /// until it is exact. Vertical sliders go native.</item>
    /// <item>date, time, month, week, datetime-local: click into the first segment of the browser's own
    /// editor and type each segment in the order the editor shows them, verified afterwards.</item>
    /// <item>color: native (the OS colour dialog is no route). Checkbox, radio, file and the other
    /// unfillable types: native, which raises exactly as Playwright does.</item>
    /// </list>
    /// Values over 200 characters are not typed (thousands of keys would crawl): the field is glided to and
    /// clicked into as above, then the value is inserted in one go (Keyboard.InsertTextAsync) and checked.
    /// Strict like Playwright's locator actions; a timeout propagates.
    /// </summary>
    public static async Task HumanFillAsync(this ILocator locator, string value, LocatorFillOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(value);
        await SerialAsync(locator.Page, async () =>
        {
            if (await RoutableAsync(locator, strict: true).ConfigureAwait(false)
                && await FillHumanizedAsync(locator, value, options?.Timeout, options?.Force == true).ConfigureAwait(false))
                return;
            await locator.FillAsync(value, options).ConfigureAwait(false);   // outside any catch: raises once
        }).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanFillAsync(ILocator,string,LocatorFillOptions)"/>
    public static async Task HumanFillAsync(this IPage page, string selector, string value, PageFillOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        ArgumentNullException.ThrowIfNull(value);
        bool strict = options?.Strict == true;
        var loc = strict ? page.Locator(selector) : page.Locator(selector).First;
        await SerialAsync(page, async () =>
        {
            if (await RoutableAsync(loc, strict).ConfigureAwait(false)
                && await FillHumanizedAsync(loc, value, options?.Timeout, options?.Force == true).ConfigureAwait(false))
                return;
            await page.FillAsync(selector, value, options).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>The humanized fill routes (the Python SDK's hfill). False = hand the fill to Playwright.</summary>
    private static async Task<bool> FillHumanizedAsync(ILocator loc, string value, float? timeout, bool force)
    {
        try
        {
            var page = loc.Page;
            var box = await BoxOfAsync(loc, timeout, force).ConfigureAwait(false);
            var info = await DescribeAsync(page, box, IsolatedWorld.FormControl).ConfigureAwait(false);
            var kind = info is not null ? Forms.ClassifyField(info.Tag, info.Type, info.Ce)
                                        : await ClassifyFallbackAsync(loc).ConfigureAwait(false);
            if (info is not null && (info.Disabled || info.ReadOnly)) return !Fallback("fill: disabled or read-only");
            bool done = kind switch
            {
                "text" => await FillTextAsync(loc, box, value, info).ConfigureAwait(false),
                "range" when box is not null => await FillRangeAsync(loc, box, info, value).ConfigureAwait(false),
                "datetime" when box is not null => await FillDatetimeAsync(loc, box, info, value).ConfigureAwait(false),
                _ => false,   // color and the unfillable types: Playwright by design
            };
            if (!done && kind is "text" or "range" or "datetime") Fallback($"fill: the {kind} route did not verify");
            return done;
        }
        catch (Exception e) when (!IsTimeoutError(e))
        {
            Fallback("fill: " + e.Message);   // native: anything the humane route could not prove it did right
        }
        return false;
    }

    /// <summary>
    /// Wait the way Playwright would, with the CALLER's timeout (null = the page's default), and return the
    /// box, in view. With <paramref name="force"/> Playwright skips actionability, so nothing is waited for: a
    /// hidden target goes native at once instead of timing out where the native call would have acted.
    /// <para>Each Playwright call that resolves the element to a handle (WaitForAsync,
    /// ScrollIntoViewIfNeededAsync, BoundingBoxAsync) makes a MAIN-world handle whose preview reads the
    /// element's textContent where the page can see it: verifier B counted 4 such reads per humanized click
    /// and 7 per textarea fill. So each is made only when needed: the wait only for an element not visible
    /// yet (IsVisibleAsync runs in Playwright's utility world), the scroll only for a box not already showing,
    /// and a plain-CSS locator needs no handle at all (CssBoxAsync). What remains, for other selectors, is
    /// the one BoundingBoxAsync.</para>
    /// </summary>
    private static async Task<LocatorBoundingBoxResult?> BoxOfAsync(ILocator loc, float? timeout, bool force)
    {
        var page = loc.Page;
        if (!await loc.IsVisibleAsync().ConfigureAwait(false))
        {
            if (force) throw new NativeRouteException("forced on a hidden target");
            await loc.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible, Timeout = timeout }).ConfigureAwait(false);
        }
        if (await CssBoxAsync(loc).ConfigureAwait(false) is { } css) return css;
        var box = await loc.BoundingBoxAsync().ConfigureAwait(false);
        if (box is not null && !(await InViewAsync(page, box).ConfigureAwait(false) && await HitOkAsync(page, box, CssOf(loc)).ConfigureAwait(false)))
        {
            // Off-screen: scroll the window to it and prove it shows there (hit test), with no new handle.
            // Clipped by a scrolling container, covered, or the window scroll did not bring it out:
            // Playwright's scroll, which also scrolls containers, and a fresh box.
            var moved = !await InViewAsync(page, box).ConfigureAwait(false) ? await ScrollWindowToAsync(page, box).ConfigureAwait(false) : null;
            if (moved is not null && await InViewAsync(page, moved).ConfigureAwait(false) && await HitOkAsync(page, moved, CssOf(loc)).ConfigureAwait(false))
                return moved;
            await loc.ScrollIntoViewIfNeededAsync(new LocatorScrollIntoViewIfNeededOptions { Timeout = timeout }).ConfigureAwait(false);
            box = await loc.BoundingBoxAsync().ConfigureAwait(false);
        }
        return box;
    }

    private static LocatorBoundingBoxResult Bb(double x, double y, double w, double h)
        => new() { X = (float)x, Y = (float)y, Width = (float)w, Height = (float)h };

    /// <summary>
    /// The plain-CSS selector a locator stands for, when it is one: <c>page.Locator("#name")</c> (and its
    /// <c>.First</c>, whose first match is the same element). Read from the locator's own description
    /// ("Locator@selector"); null for anything else (GetByRole, text=, <c>&gt;&gt;</c> chains, a frame), which
    /// then takes Playwright's BoundingBoxAsync.
    /// </summary>
    internal static string? CssOf(ILocator loc)
    {
        const string Prefix = "Locator@", First = " >> nth=0";
        var s = loc.ToString();
        if (s is null || !s.StartsWith(Prefix, StringComparison.Ordinal)) return null;
        s = s[Prefix.Length..];
        if (s.EndsWith(First, StringComparison.Ordinal)) s = s[..^First.Length];
        return s.Length == 0 || s.Contains(">>", StringComparison.Ordinal) || s.StartsWith("internal:", StringComparison.Ordinal) ? null : s;
    }

    private static LocatorBoundingBoxResult? RectBox(JsonElement? r)
    {
        if (r is not { ValueKind: JsonValueKind.Object } o || !o.TryGetProperty("rect", out var rect)
            || rect.ValueKind != JsonValueKind.Array || rect.GetArrayLength() != 4) return null;
        double x = rect[0].GetDouble(), y = rect[1].GetDouble(), w = rect[2].GetDouble(), h = rect[3].GetDouble();
        return w > 0 && h > 0 ? Bb(x, y, w, h) : null;
    }

    /// <summary>
    /// The box of a plain-CSS locator's first match, read (and scrolled into view) in the isolated world: not
    /// even the one BoundingBoxAsync handle, and its textContent preview, is made. Used only when Playwright
    /// counts exactly the elements the browser's own querySelectorAll finds -- Playwright's CSS also pierces
    /// open shadow roots, so equal counts mean the same elements in the same order. Null otherwise.
    /// </summary>
    private static async Task<LocatorBoundingBoxResult?> CssBoxAsync(ILocator loc)
    {
        if (CssOf(loc) is not { } sel) return null;
        var page = loc.Page;
        var w = IsolatedWorld.For(page);
        var r = await w.EvaluateAsync(IsolatedWorld.CssBox, new Dictionary<string, object?> { ["sel"] = sel, ["scroll"] = false })
            .ConfigureAwait(false);
        if (r is not { ValueKind: JsonValueKind.Object } o || !o.TryGetProperty("n", out var n)
            || n.ValueKind != JsonValueKind.Number || n.GetInt32() == 0) return null;
        try
        {
            if (await page.Locator(sel).CountAsync().ConfigureAwait(false) != n.GetInt32()) return null;
        }
        catch (PlaywrightException)
        {
            return null;
        }
        var box = RectBox(r);
        if (box is not null && !(await InViewAsync(page, box).ConfigureAwait(false) && await HitOkAsync(page, box, sel).ConfigureAwait(false)))
        {
            // scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does nothing when it
            // already shows (then the cover is an overlay: the click's own check)
            box = RectBox(await w.EvaluateAsync(IsolatedWorld.CssBox,
                new Dictionary<string, object?> { ["sel"] = sel, ["scroll"] = true }).ConfigureAwait(false));
        }
        return box;
    }

    /// <summary>Does the element filling <paramref name="box"/> own its centre -- not clipped by a scrolling
    /// container, not covered? The browser's own hit test, read in the isolated world.</summary>
    private static async Task<bool> HitOkAsync(IPage page, LocatorBoundingBoxResult box, string? sel = null)
        => await HitInBoxAsync(page, box, box.X + box.Width / 2.0, box.Y + box.Height / 2.0, sel).ConfigureAwait(false) == true;

    /// <summary>Is <paramref name="box"/> wholly inside the viewport?</summary>
    private static async Task<bool> InViewAsync(IPage page, LocatorBoundingBoxResult box)
    {
        var (vw, vh) = await ViewportAsync(page).ConfigureAwait(false);
        return box.X >= 0 && box.Y >= 0 && box.X + box.Width <= vw && box.Y + box.Height <= vh;
    }

    /// <summary>
    /// Scroll the WINDOW so <paramref name="box"/> sits centred on the axis where it was off-screen, the way
    /// Playwright's scroll centres it, and return where the box is now (moved by exactly the scrolled
    /// distance) -- or null. Needs no handle: the box is already known.
    /// </summary>
    private static async Task<LocatorBoundingBoxResult?> ScrollWindowToAsync(IPage page, LocatorBoundingBoxResult box)
    {
        var (vw, vh) = await ViewportAsync(page).ConfigureAwait(false);
        if (box.Width > vw || box.Height > vh) return null;
        double cx = box.X + box.Width / 2.0, cy = box.Y + box.Height / 2.0;
        double dx = box.X < 0 || box.X + box.Width > vw ? cx - vw / 2 : 0;
        double dy = box.Y < 0 || box.Y + box.Height > vh ? cy - vh / 2 : 0;
        var moved = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.ScrollBy, new[] { dx, dy }).ConfigureAwait(false);
        if (moved is not { ValueKind: JsonValueKind.Array } m || m.GetArrayLength() != 2) return null;
        return Bb(box.X - m[0].GetDouble(), box.Y - m[1].GetDouble(), box.Width, box.Height);
    }

    /// <summary>Describe the SAME element again, from the node the first hit test found: no new Playwright
    /// handle (see BoxOfAsync), so nothing the page can see. Null if it is gone.</summary>
    private static async Task<FieldInfo?> RedescribeAsync(IPage page, FieldInfo info, string? want)
    {
        var r = await IsolatedWorld.For(page).CallOnNodeAsync(info.Node, IsolatedWorld.Describe,
            new Dictionary<string, object?> { ["want"] = want }).ConfigureAwait(false);
        if (r is not { } el || FieldInfo.From(el, info.Node) is not { } again) return null;
        again.ProbeX = info.ProbeX;
        again.ProbeY = info.ProbeY;
        return again;
    }

    /// <summary>
    /// A click point the hit test proved reaches the control, when its centre did not (a floating label);
    /// null when the centre itself was fine (then the persona's own point is used, as for any click).
    /// </summary>
    private static Point? ProvenPoint(LocatorBoundingBoxResult box, FieldInfo? info)
    {
        if (info is null || (info.ProbeX == 0.5 && info.ProbeY == 0.5)) return null;
        double x = box.X + box.Width * info.ProbeX + Gauss(0, 1.5);
        double y = box.Y + box.Height * info.ProbeY + Gauss(0, 1.0);
        return new Point(Math.Min(Math.Max(x, box.X + 1), box.X + box.Width - 1),
                         Math.Min(Math.Max(y, box.Y + 1), box.Y + box.Height - 1));
    }

    /// <summary>
    /// Click into the control filling <paramref name="bb"/> (resolved, visible and in view by BoxOfAsync):
    /// glide to a persona point inside it -- or to <paramref name="at"/>, the point the hit test proved, when
    /// the centre is covered -- and click it (the Python SDK's _focus_click). Stability is checked in the
    /// isolated world -- the control must still fill the same box 50 ms later, and not be disabled -- not by a
    /// second BoundingBoxAsync, which the page could see. False when it cannot (the caller goes native).
    /// <para>A control no probe point reaches -- a label or an icon covers all of it -- is clicked anyway,
    /// where a person clicks (a floating label passes the focus on), and the caller checks that the caret
    /// arrived. Its stability then comes from a fresh box of <paramref name="loc"/>: the isolated CSS read,
    /// else BoundingBoxAsync. Requiring the hit test there sent a fully labelled field to Playwright's
    /// insertText.</para>
    /// </summary>
    private static async Task<bool> FocusClickAsync(IPage page, LocatorBoundingBoxResult? bb, Point? at, ILocator? loc)
    {
        if (bb is null) return false;
        var p = PersonaFor(page);
        var st = StateFor(page);
        await Task.Delay(50).ConfigureAwait(false);
        var info = await DescribeAsync(page, bb, IsolatedWorld.FormControl).ConfigureAwait(false);
        if (info is null)
        {
            var again = loc is null ? null
                : await CssBoxAsync(loc).ConfigureAwait(false) ?? await loc.BoundingBoxAsync().ConfigureAwait(false);
            if (again is null || Math.Abs(again.X - bb.X) > 1 || Math.Abs(again.Y - bb.Y) > 1) return false;
        }
        else if (info.Disabled) return false;
        var from = st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
        var p0 = at ?? Motion.ClickPoint(new Box(bb.X, bb.Y, bb.Width, bb.Height), from, p);
        var pt = new Point(Math.Round(p0.X), Math.Round(p0.Y));   // the pixel the hit test answered for
        await GlideAsync(page, p, st, pt.X, pt.Y, Math.Max(6, bb.Width)).ConfigureAwait(false);
        await ClickAtAsync(page, p, pt.X, pt.Y).ConfigureAwait(false);
        return true;
    }

    /// <summary>Raised inside the fill route when a field will not clear; the fill then goes native.</summary>
    private sealed class FieldNotClearedException : Exception
    {
        public FieldNotClearedException() : base("field would not clear") { }
    }

    /// <summary>
    /// Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment (40-110 ms)
    /// before the A, the A is held for the persona's dwell, the modifier comes up just after (20-80 ms). Sent
    /// as one Playwright chord the modifier and the A went down within ~1 ms of each other. ControlOrMeta
    /// lets Playwright pick the key the ENGINE's editing honours: a Meta+A sent to a Linux engine selects
    /// nothing.
    /// </summary>
    private static async Task SelectAllAsync(IPage page, Persona p)
    {
        await page.Keyboard.DownAsync("ControlOrMeta").ConfigureAwait(false);
        try
        {
            await Task.Delay((int)Rand(40, 110)).ConfigureAwait(false);
            await page.Keyboard.PressAsync("a", new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
            await Task.Delay((int)Rand(20, 80)).ConfigureAwait(false);
        }
        finally
        {
            await page.Keyboard.UpAsync("ControlOrMeta").ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Select all, then Backspace -- each key HELD like a finger holds it (a ~1 ms clear is exactly what a
    /// key-dwell check flags). If the field is still not empty (<paramref name="read"/> -- the field's value
    /// -- says so), it is emptied from the end, one Backspace per character.
    /// </summary>
    private static async Task ClearFocusedAsync(IPage page, Persona p, Func<Task<string?>> read)
    {
        await SelectAllAsync(page, p).ConfigureAwait(false);
        await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
        await KeyAsync(page, p, "Backspace").ConfigureAwait(false);
        await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
        var left = await read().ConfigureAwait(false);
        if (!string.IsNullOrEmpty(left))
        {
            int n = left.EnumerateRunes().Count();
            if (n > 64) throw new FieldNotClearedException();
            await KeyAsync(page, p, "End").ConfigureAwait(false);
            await Task.Delay((int)Rand(80, 180)).ConfigureAwait(false);
            await KeyAsync(page, p, "Backspace", n, 55, 120).ConfigureAwait(false);
            await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
            if (!string.IsNullOrEmpty(await read().ConfigureAwait(false))) throw new FieldNotClearedException();
        }
    }

    /// <summary>
    /// Type <paramref name="value"/> into a text control and PROVE it landed. The value is read through
    /// Playwright's utility world (<c>InputValueAsync</c>): it sees the control the locator means, wherever
    /// focus is, and the page cannot observe it. A field whose value cannot be read, or a click that did not
    /// put the caret in it (an overlay took it), is left to Playwright -- "could not check" is never success.
    /// A value an input mask only re-punctuated ("(555) 123-4567" for "5551234567") counts as typed right.
    /// </summary>
    private static async Task<bool> FillTextAsync(ILocator loc, LocatorBoundingBoxResult? box, string value, FieldInfo? info)
    {
        var page = loc.Page;
        var p = PersonaFor(page);
        var ceAttr = info is null ? await loc.GetAttributeAsync("contenteditable").ConfigureAwait(false) : null;
        bool isCe = info?.Ce == true || (info is null && (string.IsNullOrEmpty(ceAttr) ? "false" : ceAttr) != "false");
        // What the field can hold: a maxlength keeps the first N characters (what Playwright's insertText
        // leaves too), and a person types only those -- the field stops taking keys.
        var want = value;
        (int? Max, string? Tag, string? Type) limit = (null, null, null);
        if (!isCe)
        {
            if (info is not null) limit = (info.Maxlength, info.Tag, info.Type);
            else
            {
                var mlAttr = await loc.GetAttributeAsync("maxlength").ConfigureAwait(false);
                int? ml = int.TryParse(mlAttr, NumberStyles.Integer, CultureInfo.InvariantCulture, out var mlv) ? mlv : null;
                var tag = await loc.Locator("xpath=self::textarea").CountAsync().ConfigureAwait(false) > 0 ? "textarea" : "input";
                limit = (ml, tag, await loc.GetAttributeAsync("type").ConfigureAwait(false) ?? "");
            }
            want = Forms.FitMaxlength(value, limit.Max, limit.Tag, limit.Type);
        }
        // Bulk values (over 200 characters) are still inserted at once -- typing thousands of keys would
        // crawl -- but the way a person pastes: glide to the field, click into it, then insert.
        bool bulk = want.EnumerateRunes().Count() > 200;   // what the field holds: a 300-character value cut to 5 is typed
        async Task Put(string text, bool typos)
        {
            if (bulk)
            {
                await Task.Delay((int)Rand(120, 320)).ConfigureAwait(false);   // the paste shortcut, after the click
                await page.Keyboard.InsertTextAsync(text).ConfigureAwait(false);
            }
            else await TypeTextAsync(page, p, text, info?.Traits, typos).ConfigureAwait(false);
        }
        // the box BoxOfAsync resolved (no second BoundingBoxAsync handle the page could see)
        // lane D: from the field before it, a person Tabs (persona share, focus verified); else a click
        if (!(info is not null && await TabIntoAsync(loc, info).ConfigureAwait(false)))
        {
            if (!await FocusClickAsync(page, box, box is null ? null : ProvenPoint(box, info), loc).ConfigureAwait(false)) return false;
            await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);
            if (!await FocusedAsync(loc).ConfigureAwait(false)) return false;
        }

        async Task<string?> Read()
        {
            try
            {
                return await loc.InputValueAsync(new LocatorInputValueOptions { Timeout = CoverRetryMs }).ConfigureAwait(false);
            }
            catch (Exception)
            {
                return null;
            }
        }

        if (isCe)
        {
            // contenteditable: Playwright's fill sets text the same way; there is no value to read back
            if (!string.IsNullOrEmpty(info?.Value)) await ClearFocusedAsync(page, p, () => ActiveValueAsync(page)).ConfigureAwait(false);
            await Put(want, typos: true).ConfigureAwait(false);
            return true;
        }
        var cur = await Read().ConfigureAwait(false);
        if (cur is null) return false;
        if (cur.Length > 0) await ClearFocusedAsync(page, p, Read).ConfigureAwait(false);   // a person does not select-all an empty field
        await Put(want, typos: true).ConfigureAwait(false);
        var got = await Read().ConfigureAwait(false);
        if (got is null) return false;
        // exact; a mask that only re-punctuated what was typed (a phone format); or one that re-punctuated it and
        // then stopped taking keys (MM/YY on maxlength 5): no wipe and retype (Forms.MaskKept)
        if (got == want || Forms.FormattedEqual(got, want) || Forms.MaskKept(got, want, limit.Max, limit.Tag, limit.Type)) return true;
        if (bulk) return false;   // an inserted value that did not land: Playwright's fill, not a second paste
        // Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips, then
        // give the field to Playwright -- a correct value beats pretty input.
        await ClearFocusedAsync(page, p, Read).ConfigureAwait(false);
        await Put(want, typos: false).ConfigureAwait(false);
        got = await Read().ConfigureAwait(false);
        return got is not null && (got == want || Forms.FormattedEqual(got, want) || Forms.MaskKept(got, want, limit.Max, limit.Tag, limit.Type));
    }

    private static async Task<double?> InputNumberAsync(ILocator loc)
        => double.TryParse((await loc.InputValueAsync().ConfigureAwait(false)).Trim(), NumberStyles.Float,
               CultureInfo.InvariantCulture, out var v) ? v : null;

    /// <summary>
    /// A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it exactly.
    /// Typing digits into a slider changes nothing, so the old type-into-it route left it where it was.
    /// </summary>
    private static async Task<bool> FillRangeAsync(ILocator loc, LocatorBoundingBoxResult bb, FieldInfo? info, string value)
    {
        var page = loc.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        string? min, max, step, dir, wm;
        if (info is null)
        {
            min = await loc.GetAttributeAsync("min").ConfigureAwait(false);
            max = await loc.GetAttributeAsync("max").ConfigureAwait(false);
            step = await loc.GetAttributeAsync("step").ConfigureAwait(false);
            dir = "ltr";
            wm = "";
        }
        else
        {
            (min, max, step, dir, wm) = (info.Min, info.Max, info.Step, info.Dir, info.Wm);
        }
        // Off the step, out of range, not a number: Playwright raises "Malformed value" for these, and so
        // does this route (by leaving them to it) -- never a silently snapped or clamped value.
        if (!Forms.RangeValueOk(value, min, max, step)) return false;
        if ((wm ?? "").StartsWith("vertical", StringComparison.Ordinal)) return false;
        bool rtl = dir == "rtl";
        var (lo, hi, stepV) = Forms.RangeAttrs(min, max, step);
        double target = Forms.RangeSanitize(value, min, max, step);
        var box = new Box(bb.X, bb.Y, bb.Width, bb.Height);

        var cur = await InputNumberAsync(loc).ConfigureAwait(false);
        if (cur is double c0 && Forms.NumericEqual(c0, target)) return true;
        var (tx, ty) = Forms.RangePoint(box, Forms.RangeFraction(target, lo, hi), rtl);
        ty += Gauss(0, box.Height * 0.08);
        if (cur is double c && Rnd.NextDouble() < 0.65)
        {
            // Grab the thumb where it is and slide it: what most hands do with a slider.
            var (cx, cy) = Forms.RangePoint(box, Forms.RangeFraction(c, lo, hi), rtl);
            cy += Gauss(0, box.Height * 0.08);
            await GlideAsync(page, p, st, cx, cy, 16).ConfigureAwait(false);
            await PrePressAsync(p).ConfigureAwait(false);
            var (grabMs, releaseMs) = Motion.DragDwell(p);
            await page.Mouse.DownAsync().ConfigureAwait(false);
            try
            {
                await Task.Delay((int)Math.Round(grabMs)).ConfigureAwait(false);
                await GlideAsync(page, p, st, tx, ty, 24, settle: true).ConfigureAwait(false);
                await Task.Delay((int)Math.Round(releaseMs)).ConfigureAwait(false);
            }
            finally
            {
                // A button left down survives into every later action, so the release is unconditional.
                try { await page.Mouse.UpAsync().ConfigureAwait(false); } catch (PlaywrightException) { }
            }
        }
        else
        {
            await GlideAsync(page, p, st, tx, ty, 16).ConfigureAwait(false);
            await ClickAtAsync(page, p, tx, ty).ConfigureAwait(false);
        }
        for (int round = 0; round < 3; round++)
        {
            var v = await InputNumberAsync(loc).ConfigureAwait(false);
            if (v is not double now) return false;
            if (Forms.NumericEqual(now, target)) return true;
            var (key, n) = Forms.RangeNudge(now, target, stepV);
            if (key is null || n <= 0 || n > 40) break;
            if (rtl) key = key == "ArrowRight" ? "ArrowLeft" : "ArrowRight";
            await Task.Delay((int)Rand(150, 350)).ConfigureAwait(false);
            await KeyAsync(page, p, key, n).ConfigureAwait(false);
        }
        return await InputNumberAsync(loc).ConfigureAwait(false) is double last && Forms.NumericEqual(last, target);
    }

    /// <summary>
    /// A native date / time / month / week / datetime-local editor: click into its first field and type
    /// each field in the order the editor itself shows them (read from its UA shadow tree over CDP with
    /// DOM.describeNode pierce -- the order follows the browser, so it is never assumed).
    /// </summary>
    private static async Task<bool> FillDatetimeAsync(ILocator loc, LocatorBoundingBoxResult bb, FieldInfo? info, string value)
    {
        if (info is null || info.Node <= 0) return false;
        var page = loc.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        var kind = info.Type;
        var w = IsolatedWorld.For(page);
        var tree = await w.SendAsync("DOM.describeNode", new Dictionary<string, object>
        {
            ["backendNodeId"] = info.Node, ["depth"] = -1, ["pierce"] = true,
        }).ConfigureAwait(false);
        if (tree is not { ValueKind: JsonValueKind.Object } t || !t.TryGetProperty("node", out var node)) return false;
        var segs = Forms.SegmentNodesFromTree(node);
        var plan = Forms.PlanDatetime(kind, segs.Select(s => s.Name).ToList(), value, info.Value);
        if (plan is null || plan.Count == 0 || segs.Count == 0) return false;
        double? px = null, py = null;
        double fw = 12.0;
        if (segs[0].BackendNodeId is int segId
            && await w.SendAsync("DOM.getBoxModel", new Dictionary<string, object> { ["backendNodeId"] = segId }).ConfigureAwait(false)
                is { ValueKind: JsonValueKind.Object } bm
            && bm.TryGetProperty("model", out var model) && model.TryGetProperty("content", out var content)
            && content.ValueKind == JsonValueKind.Array && content.GetArrayLength() >= 8)
        {
            var q = content.EnumerateArray().Select(e => e.GetDouble()).ToArray();
            double x0 = new[] { q[0], q[2], q[4], q[6] }.Min(), x1 = new[] { q[0], q[2], q[4], q[6] }.Max();
            double y0 = new[] { q[1], q[3], q[5], q[7] }.Min(), y1 = new[] { q[1], q[3], q[5], q[7] }.Max();
            // trust the field box only when it lies inside the input's own box
            if (bb.X - 1 <= x0 && x0 < x1 && x1 <= bb.X + bb.Width + 1 && bb.Y - 1 <= y0 && y0 < y1 && y1 <= bb.Y + bb.Height + 1)
            {
                px = Rand(x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75);
                py = (y0 + y1) / 2 + Gauss(0, Math.Max(0.5, (y1 - y0) * 0.1));
                fw = Math.Max(6.0, x1 - x0);
            }
        }
        if (px is null)
        {
            px = bb.X + Math.Min(14.0, bb.Width * 0.12);
            py = bb.Y + bb.Height / 2;
        }
        await GlideAsync(page, p, st, px.Value, py!.Value, fw).ConfigureAwait(false);
        await ClickAtAsync(page, p, px.Value, py.Value).ConfigureAwait(false);
        await Task.Delay((int)Rand(150, 350)).ConfigureAwait(false);
        foreach (var a in plan)
        {
            if (a.Op == "type")
            {
                // The same typing model as text (persona dwell, gaussian gaps, thinking pauses, rollover):
                // the old flat 60-150 ms gap had a CV of 0.24 and no rollover. A segment moves on by itself
                // after its last digit and keys go down in order, so a rolled-over digit still lands in the
                // right field (checked on r38, all five types). No typos: a slip in a date is a wrong date.
                await TypeTextAsync(page, p, a.Arg, null, typos: false).ConfigureAwait(false);
            }
            else await KeyAsync(page, p, a.Arg, a.Count ?? 1, 70, 170).ConfigureAwait(false);
            await Task.Delay((int)Rand(60, 220)).ConfigureAwait(false);   // between fields: the eye checks what it typed
        }
        return Forms.DatetimeEqual(kind, await loc.InputValueAsync().ConfigureAwait(false), value);
    }

    /// <summary>
    /// Classify through Playwright's utility-world queries when the hit test could not prove the element
    /// (it is covered, or inside a frame): attributes only, nothing the page can see.
    /// </summary>
    private static async Task<string> ClassifyFallbackAsync(ILocator loc)
    {
        foreach (var tag in new[] { "textarea", "select" })
            if (await loc.Locator("xpath=self::" + tag).CountAsync().ConfigureAwait(false) > 0) return Forms.ClassifyField(tag);
        if (await loc.Locator("xpath=self::input").CountAsync().ConfigureAwait(false) > 0)
            return Forms.ClassifyField("input", await loc.GetAttributeAsync("type").ConfigureAwait(false));
        var ce = await loc.GetAttributeAsync("contenteditable").ConfigureAwait(false);
        return Forms.ClassifyField("div", null, ce is not null && ce.ToLowerInvariant() != "false");
    }

    // ------------------------------------------------------------------------------ check / uncheck
    /// <summary>
    /// Check a checkbox or radio with a humanized click -- only when it is not checked already -- then
    /// verify the state; anything else goes to Playwright's CheckAsync, which raises as it always does.
    /// Force and Position shape the click as in <see cref="HumanClickAsync(ILocator,LocatorClickOptions)"/>;
    /// Trial performs no action. Strict like Playwright's locator actions; a timeout propagates.
    /// </summary>
    public static async Task HumanCheckAsync(this ILocator locator, LocatorCheckOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SetCheckedAsync(locator, true, options?.Timeout, options?.Force == true, options?.Position,
            options?.Trial == true, strict: true, () => locator.CheckAsync(options)).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanCheckAsync(ILocator,LocatorCheckOptions)"/>
    public static async Task HumanCheckAsync(this IPage page, string selector, PageCheckOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        bool strict = options?.Strict == true;
        await SetCheckedAsync(strict ? page.Locator(selector) : page.Locator(selector).First, true, options?.Timeout,
            options?.Force == true, options?.Position, options?.Trial == true, strict,
            () => page.CheckAsync(selector, options)).ConfigureAwait(false);
    }

    /// <summary>
    /// Uncheck a checkbox with a humanized click -- only when it is checked -- then verify the state;
    /// otherwise as <see cref="HumanCheckAsync(ILocator,LocatorCheckOptions)"/>.
    /// </summary>
    public static async Task HumanUncheckAsync(this ILocator locator, LocatorUncheckOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SetCheckedAsync(locator, false, options?.Timeout, options?.Force == true, options?.Position,
            options?.Trial == true, strict: true, () => locator.UncheckAsync(options)).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanUncheckAsync(ILocator,LocatorUncheckOptions)"/>
    public static async Task HumanUncheckAsync(this IPage page, string selector, PageUncheckOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        bool strict = options?.Strict == true;
        await SetCheckedAsync(strict ? page.Locator(selector) : page.Locator(selector).First, false, options?.Timeout,
            options?.Force == true, options?.Position, options?.Trial == true, strict,
            () => page.UncheckAsync(selector, options)).ConfigureAwait(false);
    }

    /// <summary>
    /// Bring a checkbox or radio to <paramref name="checkedState"/> with a humanized click when it is not
    /// there already, verified afterwards; otherwise as <see cref="HumanCheckAsync(ILocator,LocatorCheckOptions)"/>.
    /// </summary>
    public static async Task HumanSetCheckedAsync(this ILocator locator, bool checkedState, LocatorSetCheckedOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await SetCheckedAsync(locator, checkedState, options?.Timeout, options?.Force == true, options?.Position,
            options?.Trial == true, strict: true, () => locator.SetCheckedAsync(checkedState, options)).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanSetCheckedAsync(ILocator,bool,LocatorSetCheckedOptions)"/>
    public static async Task HumanSetCheckedAsync(this IPage page, string selector, bool checkedState, PageSetCheckedOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        bool strict = options?.Strict == true;
        await SetCheckedAsync(strict ? page.Locator(selector) : page.Locator(selector).First, checkedState, options?.Timeout,
            options?.Force == true, options?.Position, options?.Trial == true, strict,
            () => page.SetCheckedAsync(selector, checkedState, options)).ConfigureAwait(false);
    }

    /// <summary>
    /// check / uncheck / setChecked: ONE humanized click when the state must change (that click owns its
    /// own native fallback), then the state verified. When the click did not change it -- a page that
    /// cancels the click -- this raises Playwright's own error ("Clicking the checkbox did not change its
    /// state") without clicking again, as Playwright's CheckAsync does. Errors propagate as Playwright's:
    /// nothing here may click a second time. Trial performs no action: the state is read (a non-checkbox
    /// raises, as in Playwright) and, when a click would be needed, its checks run (<see cref="TrialAsync"/>) --
    /// not Playwright's trial, which moves the pointer and presses.
    /// </summary>
    private static Task SetCheckedAsync(ILocator loc, bool want, float? timeout, bool force, Position? position,
        bool trial, bool strict, Func<Task> native)
        => SerialAsync(loc.Page, async () =>
        {
            if (!await RoutableAsync(loc, strict).ConfigureAwait(false))
            {
                await native().ConfigureAwait(false);   // strict and frame cases are Playwright's
                return;
            }
            if (await loc.IsCheckedAsync(new LocatorIsCheckedOptions { Timeout = timeout }).ConfigureAwait(false) == want) return;
            var o = new HumanClickOptions { Force = force, Position = position, Timeout = timeout, Trial = trial };
            if (trial)
            {
                await TrialAsync(loc, timeout, o.Normalize(), noClick: false).ConfigureAwait(false);
                return;
            }
            await TargetedClickAsync(loc, o, o.Normalize(), noClick: false, () => loc.ClickAsync(o.ToClick()))
                .ConfigureAwait(false);
            if (await loc.IsCheckedAsync().ConfigureAwait(false) == want) return;
            throw new PlaywrightException("Clicking the checkbox did not change its state");
        });

    // --------------------------------------------------------------------------- the element itself
    /// <summary>
    /// The control filling <paramref name="box"/> (Playwright's resolved box for a locator), read in the
    /// isolated world through the browser's own hit test: works for every Playwright selector (GetByRole,
    /// GetByLabel, <c>&gt;&gt; nth=</c>, Filter()) where document.querySelector cannot parse them. The
    /// element found must fill the SAME box (±1.5 px), or it is not the one the locator means; null when
    /// no probe point proves that. <paramref name="want"/> is the CSS selector to walk up to (null = the
    /// element hit itself).
    /// </summary>
    private static async Task<FieldInfo?> DescribeAsync(IPage page, LocatorBoundingBoxResult? box, string? want)
    {
        if (box is null || box.Width <= 0 || box.Height <= 0) return null;
        var w = IsolatedWorld.For(page);
        var scroll = await w.ScrollAsync().ConfigureAwait(false);   // once: the hit test wants document coordinates
        foreach (var (fx, fy) in ProbePoints)
        {
            var nid = await w.NodeAtAsync(box.X + box.Width * fx, box.Y + box.Height * fy, scroll).ConfigureAwait(false);
            if (nid is not int id) continue;
            var r = await w.CallOnNodeAsync(id, IsolatedWorld.Describe,
                new Dictionary<string, object?> { ["want"] = want }).ConfigureAwait(false);
            if (r is not { } el || FieldInfo.From(el, id) is not { } info) continue;
            if (Math.Abs(info.Rect[0] - box.X) <= 1.5 && Math.Abs(info.Rect[1] - box.Y) <= 1.5
                && Math.Abs(info.Rect[2] - box.Width) <= 1.5 && Math.Abs(info.Rect[3] - box.Height) <= 1.5)
            {
                // The point that PROVED the control: when the centre is covered (a floating label), this is
                // where a click actually reaches it.
                info.ProbeX = fx;
                info.ProbeY = fy;
                return info;
            }
        }
        return null;
    }

    /// <summary>What IsolatedWorld.Describe returns about one element, plus the backendNodeId it was read
    /// from and the probe point (fractions of the box) that proved it.</summary>
    private sealed class FieldInfo
    {
        public string Tag = "", Type = "";
        public bool Ce, Focused, ReadOnly, Disabled, Multiple;
        public double[] Rect = Array.Empty<double>();
        public string? Value, Min, Max, Step, InputMode, Autocomplete, Dir, Wm;
        public double Bl, Bt, ProbeX = 0.5, ProbeY = 0.5;
        public int Size, SelectedIndex = -1, Node, Maxlength = -1;
        public List<OptionInfo> Options = new();
        public List<double[]?>? OptRects;

        public FieldTraits Traits => new(Type, InputMode, Autocomplete);

        private static double[]? Quad(JsonElement e)
            => e.ValueKind == JsonValueKind.Array && e.GetArrayLength() == 4
               && e.EnumerateArray().All(x => x.ValueKind == JsonValueKind.Number)
                ? e.EnumerateArray().Select(x => x.GetDouble()).ToArray()
                : null;

        private static bool B(JsonElement e, string n) => e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.True;

        private static double D(JsonElement e, string n)
            => e.TryGetProperty(n, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;

        public static FieldInfo? From(JsonElement e, int node)
        {
            if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty("rect", out var rect) || Quad(rect) is not { } r)
                return null;
            var info = new FieldInfo
            {
                Tag = JStr(e, "tag") ?? "", Type = JStr(e, "type") ?? "", Ce = B(e, "ce"), Rect = r, Focused = B(e, "focused"),
                Value = JStr(e, "value"), Min = JStr(e, "min"), Max = JStr(e, "max"), Step = JStr(e, "step"),
                InputMode = JStr(e, "inputmode"), Autocomplete = JStr(e, "autocomplete"),
                ReadOnly = B(e, "readonly"), Disabled = B(e, "disabled"), Dir = JStr(e, "dir"), Wm = JStr(e, "wm"),
                Bl = D(e, "bl"), Bt = D(e, "bt"), Multiple = B(e, "multiple"), Size = (int)D(e, "size"),
                SelectedIndex = e.TryGetProperty("selectedIndex", out var si) && si.ValueKind == JsonValueKind.Number ? si.GetInt32() : -1,
                Node = node,
                Maxlength = e.TryGetProperty("maxlength", out var ml) && ml.ValueKind == JsonValueKind.Number ? ml.GetInt32() : -1,
            };
            if (e.TryGetProperty("options", out var opts) && opts.ValueKind == JsonValueKind.Array)
                foreach (var o in opts.EnumerateArray())
                    info.Options.Add(new OptionInfo(JStr(o, "value"), JStr(o, "label"), B(o, "disabled"), B(o, "selected")));
            if (e.TryGetProperty("optRects", out var rects) && rects.ValueKind == JsonValueKind.Array)
                info.OptRects = rects.EnumerateArray().Select(Quad).ToList();
            return info;
        }
    }
}
