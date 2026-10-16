# Offline mode (Platform Licence)

Run Clearcote PRO in isolated, air-gapped networks: no licence calls, no downloads, no telemetry. Offline mode
comes with a Platform Licence. You need two things from your account: an **Offline Key** file and an **offline
bundle** (the PRO browser plus the SDK packages).

## 1. Build the offline bundle (on a connected staging machine)

Linux / macOS staging machine:

```bash
curl -fsSL https://www.clearcotelabs.com/api/v1/offline/fetch.sh -o fetch.sh
CLEARCOTE_LICENSE_KEY=cc_lic_... bash fetch.sh linux
```

Windows staging machine:

```powershell
iwr https://www.clearcotelabs.com/api/v1/offline/fetch.ps1 -OutFile fetch.ps1
$env:CLEARCOTE_LICENSE_KEY = "cc_lic_..."; .\fetch.ps1 -Platform windows
```

The folder it creates holds `clearcote-offline-key.json`, the PRO browser archive (sha256-verified), the SDK
packages with their dependencies (`sdk/python`, `sdk/node`) and a `SHA256SUMS` file. `pip download` fetches wheels
for the staging machine's own Python and OS, so use the same OS and Python version as the offline machines.
Move the folder into the isolated network with your normal transfer process and check it with `sha256sum -c SHA256SUMS`.

## 2. Install on the offline machine

```bash
tar -xJf browser/clearcote-pro-*-linux-x64.tar.xz -C /opt/clearcote
pip install --no-index --find-links sdk/python clearcote          # Python
npm install --offline ./sdk/node/node_modules/clearcote           # Node (or copy node_modules)
```

## 3. Launch

Python:

```python
import clearcote
browser = clearcote.launch(offline_key="/etc/clearcote/clearcote-offline-key.json",
                           executable_path="/opt/clearcote/chrome")
```

Node:

```js
import { launch } from "clearcote";
const browser = await launch({ offlineKey: "/etc/clearcote/clearcote-offline-key.json", executablePath: "/opt/clearcote/chrome" });
```

.NET:

```csharp
var browser = await Clearcote.LaunchAsync(new LaunchOptions {
    OfflineKey = "/etc/clearcote/clearcote-offline-key.json", ExecutablePath = "/opt/clearcote/chrome" });
```

Or set `CLEARCOTE_OFFLINE_KEY=/etc/clearcote/clearcote-offline-key.json` and `CLEARCOTE_BINARY=/opt/clearcote/chrome`
and leave the code unchanged. In offline mode the SDK makes no licence calls and never downloads; it reads the key,
checks its end date and hands the key's token to the browser, which verifies it locally.

Without the SDK (plain Playwright): launch the PRO binary with the environment variable `CLEARCOTE_RUN_TOKEN` set to
the key file's `token` value.

## 4. Replacing the key at offline sites

Each key is valid for the paid period plus 30 days. On every renewal a new key is emailed to you and is available
in the Offline card on your Licenses page (or `GET /api/v1/offline/key` with your licence key).

1. Download the new key on a connected machine as soon as the renewal email arrives.
2. Move it into each offline environment with your normal transfer process.
3. Replace the old file at the path your launch uses (`offline_key` / `CLEARCOTE_OFFLINE_KEY`). No restart of
   anything else is needed: the next browser launch uses the new key.
4. Check it: `python -c "import json;print(json.load(open('/etc/clearcote/clearcote-offline-key.json'))['expires_at'])"`.

Rules the browser enforces locally:

- **Always install the newest key.** Once a machine has used a key, it refuses any key issued before it.
- **Expiry is final.** After `expires_at` the browser does not start until a newer key is installed. The SDK warns on
  stderr from 7 days before expiry (`quiet=True` silences it).
- **Clock.** A key is valid from two days before it was issued, so a machine whose clock runs a little behind
  accepts it. A clock that runs far ahead makes a key look expired early: keep offline machines on correct time.

One organisation-wide key works on every machine and site; there is no per-machine activation.

## Troubleshooting

| Message | Meaning |
| --- | --- |
| `This offline key expired on …` | Install the newest key from your renewal. |
| `Offline mode runs the PRO binary from your offline bundle` | Pass `executable_path` / `ExecutablePath` or set `CLEARCOTE_BINARY`. |
| `… is not a Clearcote offline key file` / `has no valid token` | The file was changed or truncated in transfer; compare it with `SHA256SUMS`. |
| Browser exits: `no valid license lease for this run` | The token was refused: an older key than one this machine already used, an expired key, or an edited file. |
