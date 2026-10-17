# MidiBakery

By Klirrwerk. AI composition and session control for Ableton Live 12 Suite.

See [docs/KONZEPT.md](docs/KONZEPT.md) for the concept and roadmap, and [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md) for how the Remote Script, the core (MCP server, API, agent) and the Max for Live devices work together.

## Layout

| Path | What |
|---|---|
| `packages/core` | `midibakery` Python package: op catalog, bridge client, composition engine (`compose/`), agent loop (`agent/`), local API for Max for Live (`api/`), MCP server, CLI |
| `packages/remote_script/MidiBakery` | Remote Script loaded by Live (stdlib only, Python 3.11) |
| `packages/m4l` | Max for Live devices: build script and sources (chat device, MIDI Tools, reference analysis) |
| `packaging/` | Packaged app (PyInstaller), macOS .pkg, Windows setup, release tools (signed update manifest) |
| `services/updates` | Update and license service: Cloudflare Worker + R2 + D1 on `updates.klirrwerk.com` |
| `tests/` | Contract tests: real Remote Script + fake Live Object Model over a real socket |

## Development

```bash
uv sync
uv run pytest
uv run midibakery manifest          # after changing the catalog
uv run midibakery install --link    # Remote Script (symlinked), devices and the API's launch into Live
uv run midibakery ping              # with Live running and MidiBakery selected as Control Surface
```

### Packaged app

```bash
uv run --group package python packaging/build_app.py  # build/app/midibakery/ + MidiBakery-<version>-<os>-<arch>.zip
python packaging/smoke.py build/app/midibakery        # runs it: install/uninstall, API, MCP server
```

PyInstaller freezes `midibakery` with its Python and the payload (Remote Script, built devices) into one
folder; `midibakery install` from there installs into Live like from the checkout. It builds for the system it
runs on: CI (`.github/workflows/package.yml`) builds and smoke-tests macOS (Apple silicon) and Windows.

```bash
python packaging/macos/build_pkg.py   # build/app/MidiBakery-<version>-macos-arm64.pkg, -macos-x64.pkg on Intel (not signed yet)
```

The macOS installer installs for the current user, no administrator: the app into
`~/Library/Application Support/MidiBakery/runtime/<version>/`, then its postinstall runs `midibakery install`.
Installing a newer package updates and keeps the API's settings; `.../runtime/<version>/midibakery uninstall`
removes the app, the Remote Script, the devices and the package receipt. CI installs and uninstalls it on macOS;
the Intel package is built and tested on release tags and on PRs labelled `intel`.

```bash
python packaging/windows/build_setup.py   # on Windows, with Inno Setup 6: build/app/MidiBakery-<version>-windows-x64-setup.exe
```

The Windows setup does the same for the current user, no administrator: the app into
`%LOCALAPPDATA%\Programs\MidiBakery\runtime\<version>\`, then `midibakery install`. Running a newer setup updates;
its uninstaller (Settings > Apps) runs `midibakery uninstall` and removes the app. CI installs, updates and
uninstalls it on Windows. Neither installer is signed yet.

### Updates

The installer is the updater. The running API asks `updates.klirrwerk.com` once a day whether a newer
version exists; the devices' About line and the Chat device then show it, and *Install* in the Chat device
downloads the installer, checks it against the release's Ed25519-signed manifest and opens it.
`midibakery update` does the same from the terminal (`--check` only asks, `--disable` turns the daily check off).
A tag `v<version>` publishes a release; setup and details in [services/updates/README.md](services/updates/README.md).

### License

A 30-day trial, then a license key (perpetual, 12 months of updates, two computers). The Chat device and
`midibakery license` start the trial and activate a key; the app checks the signed license offline.
Without one it still reads Live and undoes, but generates and edits nothing. Licensing stays off until a
public key is in `LICENSE_KEYS` (`midibakery/license.py`); see [services/updates/README.md](services/updates/README.md).

### Chat and local API

```bash
uv run midibakery chat                                  # Claude (claude-sonnet-5-5) in the terminal
uv run midibakery chat --model claude-opus-5-5
uv run midibakery chat --provider ollama --model qwen3:8b  # local: all tools, compact schemas (--small for 8k context)
uv run midibakery chat --provider lmstudio --model google/gemma-4-e4b  # LM Studio server (LMSTUDIO_HOST, default 127.0.0.1:1234)
uv run midibakery api                                   # HTTP/WebSocket API for the Max for Live devices
```

Claude needs an API key: click **Key** in the chat (device or browser) or run `uv run midibakery auth set-key`. It is checked with the Claude API and stored in the macOS Keychain (service "MidiBakery") or the Windows Credential Manager, so the API that Live starts finds it too; `auth status` / `auth remove` show or delete it. `ANTHROPIC_API_KEY` and an `ant auth login` profile work as well. The API listens on
127.0.0.1:8766 and writes its port and token to `api.json` in the MidiBakery config folder.

### Max for Live devices

```bash
uv run midibakery install --api-provider lmstudio  # builds the devices, installs them and the Remote Script
uv run midibakery uninstall                         # removes exactly that again (--purge: settings and key too)
```

`install` finds the User Library Live uses (its `Library.cfg`; `--library` overrides), copies the Remote Script
and the devices there and records every path in `installed.json` in the config folder: the next install removes
what a new version no longer has, `uninstall` removes only those paths. The installers run the same command.

The Remote Script starts the API when Live loads it and stops it when Live quits (`launcher.json` and
`api.log` in the config folder; `--no-api` turns this off). An API that already runs on the port is used as is,
unless it is another MidiBakery version (left from before an update): that one is stopped and this one started.
Live does not see your shell's environment; that is why the Claude key lives in the Keychain / Credential Manager.
Each device's *About* button shows its version next to the API's and the Remote Script's.

- **MidiBakery Chat** (Max for Live > Max MIDI Effect > MidiBakery): chat with the agent in the device view;
  MIDI passes through. *Pop out* opens the same chat in the browser.
- **MidiBakery Generator / Transform** (clip view, Generate / Transform panels, Live 12.2+): the composition
  engine on the selected time range, in the clip's scale. MIDI Tools must answer synchronously, so the first
  apply shows "Working..." and the result follows a moment later in the same apply cycle.
- **MidiBakery Reference Analyzer** (Max for Live > Max Audio Effect > MidiBakery): put it on an audio track that holds a
  reference song in the Arrangement and press *Analyze*. A warped clip is analyzed on Live's bars. An unwarped one
  plays at its own tempo, so its bars drift away from the Set's: the device detects its tempo and first downbeat,
  warps the clip onto them (bar 1 on a bar line of the Arrangement, the Set's tempo unchanged) and analyzes it
  again. If that fails it says so: warp it by hand (1.1.1 on the first downbeat), then *Analyze*. plan_song refuses
  an unwarped reference unless the song plays at the reference's tempo. The *Genre* menu
  (Auto, or one of plan_song's genres) sets the section names (e.g. Lift/Peak for Peak Time Techno), the phrase
  length (16 bars for Drum & Bass) and settles half or double time. *Sensitivity* (0-100 per lane of the view:
  Kick, Lows, Mids, Hats; 50 by default) sets how finely the song is split. Their mean works like one dial: at 100 every
  change found is a section, at 50 neighbours of the same kind are one section (four breaks in a row are one long
  break), lower a drop and its groove become one drop. Their ratio sets what counts as a change: Hats at 30 with
  the rest at 100 lets hi-hat changes pass, Kick alone splits the song where the kick stops and comes back. The analysis runs again
  once the fields rest. With *Lock* on, changing one field moves the others by as much. The changes within a
  merged section stay in the analysis as its phrases (ticks in the view, `phrases` in `get_reference`). It shows the sections
  (intro, build, drop, break, outro), key, tempo and per-bar lanes for kick, lows, mids and hats; *Set Locators*
  sets Arrangement locators at the sections, *Remove Locators* removes all locators within the clip. Runs fully inside Live (Node for Max; decoding via macOS
  `afconvert`, elsewhere WAV only). Results land in `references/` of the MidiBakery folder, where the MCP
  tool `get_reference` reads them.

### Claude Code / Claude Desktop

```bash
claude mcp add midibakery -- uv --directory /Users/maxx/Development/Ableton/MidiBakery run midibakery mcp
```

## License

Proprietary, all rights reserved. The source is publicly visible, but no right to use, copy, modify or distribute it is granted; see [LICENSE](LICENSE). Ableton and Ableton Live are trademarks of Ableton AG; this project is not affiliated with Ableton.
