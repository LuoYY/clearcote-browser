# AGENTS.md — guide for automated tools and contributors

Agents open this repository for one of two reasons:

- **To use Clearcote in a project** (scraping, testing, an AI agent that needs a browser): read [Using Clearcote in a project](#using-clearcote-in-a-project).
- **To work on Clearcote itself** (patches, build, SDK code): skip to [What this project is](#what-this-project-is).

## Using Clearcote in a project

Clearcote is a Chromium build with its fingerprint controls in the engine. It is driven by Playwright,
Puppeteer or any Chrome DevTools Protocol (CDP) client. Install the SDK; it downloads the browser on first
use and checks its SHA-256, so `playwright install` is not needed.

```bash
pip install clearcote          # Python
npm install clearcote          # Node.js / TypeScript
dotnet add package Clearcote   # .NET
```

```python
from clearcote import launch          # asyncio: from clearcote.async_api import launch

browser = launch(fingerprint="user-7423", platform="windows")   # returns a standard Playwright Browser
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

- **Keep the existing Playwright code.** Replace only the `chromium.launch(...)` call with `launch(...)`.
- **Tools that take a CDP URL** (browser-use, Crawl4AI, Playwright MCP, Puppeteer): start an endpoint with
  `serve()` or `clearcote-serve --port 9222`, then connect to `http://127.0.0.1:9222`.
- **One seed per identity.** The same `fingerprint` string returns the same machine on every launch; a new
  string gives a new, unlinked one.
- **Do not add stealth plugins** (`puppeteer-extra-plugin-stealth`, `playwright-stealth`). They patch values
  from JavaScript and conflict with the persona set in the engine.
- **Selenium and WebDriver are not supported.** Clearcote is driven over CDP.
- **Hosted browsers:** `launch(cloud=True, country="us")` with `CLEARCOTE_API_KEY` set runs the browser on
  Clearcote's servers and returns the same Playwright `Browser`.

To give an assistant (Claude, Cursor, Cline) a browser over MCP:

```json
{ "mcpServers": { "clearcote": { "command": "npx", "args": ["-y", "clearcote-mcp"] } } }
```

References: [`llms.txt`](llms.txt) · SDKs: [Python](sdk/python/README.md), [Node](sdk/node/README.md),
[.NET](sdk/dotnet) · MCP server: [`mcp/`](mcp/) · Docker: [`docker/`](docker/) · docs: <https://www.clearcotelabs.com/docs>

---

## What this project is

Clearcote Browser: an open-source, reproducible Chromium build with engine-level privacy/identity controls. Base is ungoogled-chromium; changes are delivered as readable source patches. Builds target Windows x64 and Linux x64 and are produced on a Linux host (native for Linux, cross-compiled for Windows).

## Repository layout

```
clearcote-browser/
├── README.md            # what/why, quickstart, credits
├── llms.txt             # summary for AI tools
├── ROADMAP.md           # phased plan
├── AGENTS.md            # this file
├── CONTRIBUTING.md      # contribution workflow
├── CREDITS.md           # upstream attributions + licenses
├── DISCLAIMER.md        # responsible use + legal
├── SECURITY.md          # reporting vulnerabilities
├── UPSTREAM_REVISION    # pinned Chromium revision the patches target
├── build.sh             # one-shot Linux cross-build orchestrator
├── Dockerfile           # reproducible build environment
├── scripts/             # numbered build stages (00-05) + release and CI checks
├── patches/             # Clearcote's source patches (unified diffs) + series
├── config/              # args.gn and build configuration
├── sdk/                 # SDKs: python/, node/, dotnet/ (published as `clearcote` / `Clearcote`)
├── mcp/                 # MCP server (`clearcote-mcp`)
├── docker/              # the CDP-endpoint Docker image
├── tools/               # fingerprint collector, canvas bridge server
└── docs/
    ├── BUILDING.md          # build from source
    ├── VERIFY.md            # verify a release
    ├── RELEASING.md         # maintainer release runbook (canonical)
    ├── PATCHES.md           # patch-set overview
    ├── RESEARCH.md          # design research
    └── RESEARCH-DOSSIER.md  # deep build/stealth dossier
```

## Conventions

- **Patches over forks.** Changes to Chromium are unified diffs under `patches/`, listed in `patches/series`, applied with the ungoogled `patches.py` tooling. Keep one logical change per patch with a descriptive name and a header comment explaining *why*.
- **Pin everything.** Patches target the revision in `UPSTREAM_REVISION`. If you bump it, re-test the whole series and update both together.
- **Engine-level, not JS injection.** Identity/privacy behavior belongs in compiled code, not in injected scripts.
- **No secrets, no blobs.** Never commit credentials, tokens, or opaque binaries. The tree is source + text patches only.
- **Privacy-relevant patches are mandatory.** Do not drop a privacy/identity patch to make a build pass; fix the patch.

## Common commands

```bash
# Apply the full patch set onto a prepared Chromium source tree
python3 <ungoogled>/utils/patches.py apply build/src ./patches

# Configure + build
cp config/args.gn build/src/out/Default/args.gn
( cd build/src && gn gen out/Default && ninja -C out/Default chrome )

# Hash an artifact
sha256sum out/Default/<artifact>
```

See [docs/BUILDING.md](docs/BUILDING.md) for the complete pipeline and [docs/VERIFY.md](docs/VERIFY.md) for verification.

## Adding or fixing a patch

1. Make the source change in `build/src`.
2. Generate a clean unified diff and place it under `patches/` with a clear name.
3. Add it to `patches/series` in the right order.
4. Re-run a full apply on a clean tree to confirm it applies without rejects.
5. Document the intent in the patch header and your PR.

## Ground rules for automated contributors

- Prefer small, reviewable PRs with a clear description of intent and verification steps.
- Never introduce network calls that phone home or fetch unverified code at runtime.
- If a change affects what the browser exposes to websites, say so explicitly and explain the privacy reasoning.
- When in doubt, open an issue to discuss before large changes.
