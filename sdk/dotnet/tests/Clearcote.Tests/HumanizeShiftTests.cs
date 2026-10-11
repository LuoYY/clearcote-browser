using System.Reflection;
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
        public Func<string, bool>? UnknownKey { get; set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method is null) return null;
            var onKeyboard = method.DeclaringType == typeof(IKeyboard);
            switch (method.Name)
            {
                case "get_Keyboard": return Keyboard;
                case "get_Mouse": return Mouse;
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
}
