using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// Capitals and shifted symbols go behind a real ShiftLeft (mirrors the Python and Node tests). r28 sent
// "A" and "!" with shiftKey=false and no Shift keydown at all.
public class HumanizeShiftTests
{
    // A recording stand-in for IPage / IKeyboard / IMouse: keyboard calls are logged in order, every
    // other member returns a completed task or a default value.
    public class Recorder : DispatchProxy
    {
        public List<(string Op, string Arg)> Log { get; } = new();
        public object? Keyboard { get; set; }
        public object? Mouse { get; set; }
        public object? Context { get; set; }   // an IBrowserContext whose NewCDPSessionAsync answers (null: no isolated world)
        public Func<string, bool>? UnknownKey { get; set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method is null) return null;
            var onKeyboard = method.DeclaringType == typeof(IKeyboard);
            switch (method.Name)
            {
                case "get_Keyboard": return Keyboard;
                case "get_Mouse": return Mouse;
                case "get_Context": return Context;
                case "PressAsync" when onKeyboard:
                    var key = (string)args![0]!;
                    if (UnknownKey?.Invoke(key) == true)
                        return Task.FromException(new PlaywrightException($"Unknown key: \"{key}\""));
                    Log.Add(("press", key));
                    return Task.CompletedTask;
                case "DownAsync" when onKeyboard:
                    Log.Add(("down", (string)args![0]!));
                    return Task.CompletedTask;
                case "UpAsync" when onKeyboard:
                    Log.Add(("up", (string)args![0]!));
                    return Task.CompletedTask;
                case "TypeAsync" when onKeyboard:
                    Log.Add(("type", (string)args![0]!));
                    return Task.CompletedTask;
            }
            return DefaultFor(method.ReturnType);
        }

        private static object? DefaultFor(Type t)
        {
            if (t == typeof(Task)) return Task.CompletedTask;
            if (t.IsGenericType && t.GetGenericTypeDefinition() == typeof(Task<>))
            {
                var inner = t.GetGenericArguments()[0];
                var value = inner.IsValueType ? Activator.CreateInstance(inner) : null;
                return typeof(Task).GetMethod(nameof(Task.FromResult))!.MakeGenericMethod(inner)
                    .Invoke(null, new[] { value });
            }
            return t.IsValueType && t != typeof(void) ? Activator.CreateInstance(t) : null;
        }
    }

    private static (IPage Page, Recorder Keyboard) FakePage()
    {
        var keyboard = DispatchProxy.Create<IKeyboard, Recorder>();
        var mouse = DispatchProxy.Create<IMouse, Recorder>();
        var page = DispatchProxy.Create<IPage, Recorder>();
        var pageRec = (Recorder)(object)page;
        pageRec.Keyboard = keyboard;
        pageRec.Mouse = mouse;
        // These tests assert exact key sequences: no fat-finger slips (FormsTests and the live suite cover those).
        Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        return (page, (Recorder)(object)keyboard);
    }

    private static readonly string[] Watched = { "Shift", "A", "b", "!", "?", "c", "B", "a" };

    [Fact]
    public async Task Holds_shift_around_capitals_and_symbols_across_a_run()
    {
        var (page, kb) = FakePage();
        await page.HumanTypeAsync("Ab!?c");
        Assert.Equal(new (string, string)[]
        {
            ("down", "Shift"), ("press", "A"), ("up", "Shift"),
            ("press", "b"),
            ("down", "Shift"), ("press", "!"), ("press", "?"), ("up", "Shift"),
            ("press", "c"),
        }, kb.Log.Where(e => Watched.Contains(e.Arg)).ToArray());
    }

    [Fact]
    public async Task Releases_shift_when_the_text_ends_on_a_capital()
    {
        var (page, kb) = FakePage();
        await page.HumanTypeAsync("aB");
        Assert.Equal(new (string, string)[] { ("press", "B"), ("up", "Shift") }, kb.Log.TakeLast(2).ToArray());
    }

    [Fact]
    public async Task Inserts_characters_the_us_layout_cannot_press()
    {
        var (page, kb) = FakePage();
        kb.UnknownKey = k => k == "ö";
        await page.HumanTypeAsync("aö");
        Assert.Equal(new (string, string)[] { ("press", "a"), ("type", "ö") }, kb.Log.ToArray());
    }

    [Fact]
    public async Task Types_a_surrogate_pair_as_one_character()
    {
        var (page, kb) = FakePage();
        await page.HumanTypeAsync("\U0001F600");
        Assert.Equal(new (string, string)[] { ("press", "\U0001F600") }, kb.Log.ToArray());
    }

    [Fact]
    public void Needs_shift_is_the_us_layout()
    {
        foreach (var c in "AZ~!@#$%^&*()_+{}|:\"<>?") Assert.True(Humanize.NeedsShift(c.ToString()), c.ToString());
        foreach (var c in "az09`-=[];',./ \t\\ö") Assert.False(Humanize.NeedsShift(c.ToString()), c.ToString());
    }

    // ------------------------------------------------------------------ SC 239: capitals on the typo word-fix paths
    // A keyboard that keeps the field's value the way a browser would (a word delete under Control / Alt), so the
    // typo path can measure what landed and put a word back; the value is read through ValueCdp (ActiveValue).
    public class TypingKeyboard : Recorder
    {
        public readonly System.Text.StringBuilder Value = new();
        private readonly HashSet<string> _held = new();
        private static readonly string[] NonTyping = { "Control", "Alt", "Meta", "ControlOrMeta" };

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.DeclaringType == typeof(IKeyboard) && args is { Length: > 0 } && args[0] is string k)
            {
                switch (method.Name)
                {
                    case "PressAsync": Apply(k); break;
                    case "DownAsync": if (k.Length > 1) _held.Add(k); else Apply(k); break;
                    case "UpAsync": _held.Remove(k); break;
                }
            }
            return base.Invoke(method, args);
        }

        private void Apply(string k)
        {
            if (k == "Backspace")
            {
                if (_held.Contains("Control") || _held.Contains("Alt"))
                {
                    int i = Value.Length;
                    while (i > 0 && Value[i - 1] == ' ') i--;
                    while (i > 0 && Value[i - 1] != ' ') i--;
                    Value.Length = i;
                }
                else if (Value.Length > 0) Value.Length--;
            }
            else if (k.Length == 1 && !_held.Overlaps(NonTyping)) Value.Append(k);
        }
    }

    // The isolated world of that page: ActiveValue reads the keyboard's value, ActiveField a text input, the rest null.
    public class ValueCdp : DispatchProxy
    {
        public Func<string>? Value { get; set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            var name = (string)args![0]!;
            var expr = (args.Length > 1 ? args[1] as Dictionary<string, object> : null)?.GetValueOrDefault("expression") as string;
            string json;
            if (name == "Page.getFrameTree") json = "{\"frameTree\":{\"frame\":{\"id\":\"MAIN\"}}}";
            else if (name == "Page.createIsolatedWorld") json = "{\"executionContextId\":5}";
            else if (expr == $"({IsolatedWorld.ActiveValue})(null)") json = $"{{\"result\":{{\"value\":{JsonSerializer.Serialize(Value?.Invoke())}}}}}";
            else if (expr == $"({IsolatedWorld.ActiveField})(null)") json = "{\"result\":{\"value\":{\"type\":\"text\",\"inputmode\":null,\"autocomplete\":null}}}";
            else json = "{\"result\":{\"value\":null}}";
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());
        }
    }

    private static (IPage Page, TypingKeyboard Keyboard) TypingPage()
    {
        var keyboard = DispatchProxy.Create<IKeyboard, TypingKeyboard>();
        var kb = (TypingKeyboard)(object)keyboard;
        var mouse = DispatchProxy.Create<IMouse, Recorder>();
        var cdp = DispatchProxy.Create<ICDPSession, ValueCdp>();
        ((ValueCdp)(object)cdp).Value = () => kb.Value.ToString();
        var ctx = DispatchProxy.Create<IBrowserContext, IsolatedWorldTests.FakeContext>();
        ((IsolatedWorldTests.FakeContext)(object)ctx).Cdp = cdp;
        var page = DispatchProxy.Create<IPage, Recorder>();
        var pageRec = (Recorder)(object)page;
        pageRec.Keyboard = keyboard;
        pageRec.Mouse = mouse;
        pageRec.Context = ctx;
        Humanize.Attach(page, "sc239");
        Humanize.RolloverOverride.AddOrUpdate(page, new StrongBox<double>(0));
        Humanize.TypoRateOverride.AddOrUpdate(page, new StrongBox<double>(1.0));   // every eligible key slips
        Humanize.WordFixOverride.AddOrUpdate(page, new StrongBox<double>(1.0));    // every slip inside a word: the word fix
        return (page, kb);
    }

    [Fact]
    public async Task A_capital_put_back_after_the_word_fix_goes_behind_a_real_shift()
    {
        // SC 239 (lab contenteditable, SC 209 row "Capitals typed with Shift held 12/14"): a keydown 'M' right after
        // Control+Backspace had no Shift keydown before it. RestoreAsync (the word put back after the word-delete fix)
        // and the late-notice keys of DoTypoAsync pressed every key bare, while the type loop holds a real ShiftLeft
        // around a capital. "Mr" repeated: every 'r' slips, a substitution (seven in ten) is fixed with the word
        // delete and the "M" is put back -- behind Shift, as the loop types it.
        var (page, kb) = TypingPage();
        const string text = "Mr Mr Mr Mr Mr Mr Mr Mr Mr Mr";
        await page.HumanTypeAsync(text);
        Assert.Equal(text, kb.Value.ToString());
        var log = kb.Log;
        Assert.Contains(log, e => e.Op == "down" && e.Arg == "Control");   // at least one word fix in ten words
        var held = new HashSet<string>();
        bool afterWordDelete = false;
        int putBack = 0;
        for (int i = 0; i < log.Count; i++)
        {
            var (op, key) = log[i];
            if (op == "down") held.Add(key);
            else if (op == "up") { held.Remove(key); if (key == "Control") afterWordDelete = true; }
            else if (op == "press" && afterWordDelete)
            {
                // the first key after the word delete is the word put back: its "M"
                afterWordDelete = false;
                putBack++;
                var around = string.Join(" ", log.Skip(Math.Max(0, i - 4)).Take(7).Select(e => e.Op + ":" + e.Arg));
                Assert.True(key == "M" && held.Contains("Shift"), $"the M put back was pressed without Shift: {around}");
            }
        }
        Assert.True(putBack > 0);
    }
}
