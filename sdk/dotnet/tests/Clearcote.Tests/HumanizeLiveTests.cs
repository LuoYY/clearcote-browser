using Xunit;

namespace Clearcote.Tests;

/// Live humanize checks (same gate as GeometryLiveTests: skipped unless CLEARCOTE_LIVE_ENGINE points at a
/// chrome binary). A page that hooks the DOM prototypes humanize used to call must see none of its reads,
/// the keyboard select route must still land on the right option, and capitals must come with ShiftLeft.
public class HumanizeLiveTests
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    private const string HookPage = @"<!doctype html><html><head><meta charset='utf-8'><script>
window.__hits = [];
for (const [obj, name] of [[Document.prototype, 'querySelector'], [Document.prototype, 'elementFromPoint'],
                           [Element.prototype, 'getBoundingClientRect'], [Element.prototype, 'contains']]) {
  const orig = obj[name];
  obj[name] = function (...a) { window.__hits.push(name); return orig.apply(this, a); };
}
for (const p of ['activeElement']) {
  const d = Object.getOwnPropertyDescriptor(Document.prototype, p);
  Object.defineProperty(Document.prototype, p, { get() { window.__hits.push(p); return d.get.call(this); } });
}
for (const p of ['selectedIndex', 'options']) {
  const d = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, p);
  Object.defineProperty(HTMLSelectElement.prototype, p, { get() { window.__hits.push(p); return d.get.call(this); } });
}
window.__keys = [];
</script></head><body>
<input id='t' style='width:400px;font-size:20px;margin:100px'>
<select id='s' style='margin:20px'><option value='a'>A</option><option value='b'>B</option><option value='c'>C</option></select>
<script>document.getElementById('t').addEventListener('keydown', e => window.__keys.push([e.key, e.code, e.shiftKey]));</script>
</body></html>";

    // No Skippable* package (see GeometryLiveTests): an unset CLEARCOTE_LIVE_ENGINE makes this a no-op.
    [Fact]
    public async Task Humanize_reads_are_invisible_to_the_page_and_select_and_shift_still_work()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var dir = Path.Combine(Path.GetTempPath(), "cc-live-hz-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        try
        {
            var context = await Clearcote.LaunchPersistentContextAsync(dir, new LaunchOptions
            {
                ExecutablePath = LiveExe, Args = new[] { "--no-sandbox" }, Quiet = true,
            });
            try
            {
                var page = context.Pages.Count > 0 ? context.Pages[0] : await context.NewPageAsync();
                // the key sequence below is asserted exactly: no fat-finger slips on this page
                Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
                await page.SetContentAsync(HookPage);
                await page.Locator("#t").HumanClickAsync();
                await page.HumanTypeAsync("Ab!");
                var picked = await page.Locator("#s").HumanSelectOptionAsync("c");

                Assert.Equal(new[] { "c" }, picked);
                Assert.Equal("c", await page.EvaluateAsync<string>("document.getElementById('s').value"));
                Assert.Equal("Ab!", await page.EvaluateAsync<string>("document.getElementById('t').value"));
                var hits = await page.EvaluateAsync<string[]>("window.__hits");
                Assert.Empty(hits);
                var keys = await page.EvaluateAsync<string>("JSON.stringify(window.__keys)");
                Assert.StartsWith("[[\"Shift\",\"ShiftLeft\",true],[\"A\",\"KeyA\",true],[\"b\",\"KeyB\",false]," +
                                  "[\"Shift\",\"ShiftLeft\",true],[\"!\",\"Digit1\",true]", keys);
            }
            finally
            {
                await context.CloseAsync();
            }
        }
        finally
        {
            TestTemp.Remove(dir);
        }
    }
}
