using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Lane F (SC 232) live: a HIDDEN custom checkbox (opacity 0, zero size) is checked by a trusted click on its
/// &lt;label&gt; (for= and a wrapping label). The old route clicked the input's own box: it waited for the hidden
/// input to become visible and timed out. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
public class HumanizeLabelLiveTests : IClassFixture<FormsLiveBrowser>
{
    internal const string Html = @"<!doctype html><html><head><meta charset=utf-8><script>
window.__ev = [];
for (const t of ['mousedown','mouseup','click','keydown','keyup','input'])
  document.addEventListener(t, (e) => window.__ev.push({ t, id: (e.target && e.target.id) || '', tr: e.isTrusted,
    ts: performance.now(), key: e.key || '' }), true);
</script><style>.vh{position:absolute;opacity:0;width:0;height:0;margin:0} body{margin:20px;font:15px sans-serif}</style></head>
<body><input type=checkbox id=hcb class=vh><label for=hcb id=hlab>Accept the terms</label><br><br>
<label id=wrap><input type=checkbox id=hcb2 class=vh>Subscribe to news</label><br><br>
<input id=t value='prefilled text'><br><br><button id=btn style='width:160px;height:40px'>Button</button></body></html>";

    private readonly FormsLiveBrowser _b;
    public HumanizeLabelLiveTests(FormsLiveBrowser b) => _b = b;

    internal static async Task<List<JsonElement>> Ev(IPage page, string t)
    {
        var all = await page.EvaluateAsync<JsonElement>("window.__ev");
        return all.EnumerateArray().Where(e => e.GetProperty("t").GetString() == t).Select(e => e.Clone()).ToList();
    }

    [Theory]
    [InlineData("#hcb")]
    [InlineData("#hcb2")]
    public async Task Hidden_custom_checkbox_is_checked_through_its_label(string sel)
    {
        if (_b.Context is null) return;
        var page = await _b.Context.NewPageAsync();
        try
        {
            Humanize.Attach(page, "label-live");
            await page.SetContentAsync(Html);
            await page.Locator(sel).HumanCheckAsync(new LocatorCheckOptions { Timeout = 3000 });
            Assert.True(await page.Locator(sel).IsCheckedAsync());
            var downs = await Ev(page, "mousedown");
            Assert.Single(downs);
            Assert.True(downs[0].GetProperty("tr").GetBoolean());
            await page.Locator(sel).HumanUncheckAsync(new LocatorUncheckOptions { Timeout = 3000 });
            Assert.False(await page.Locator(sel).IsCheckedAsync());
        }
        finally
        {
            await page.CloseAsync();
        }
    }
}
