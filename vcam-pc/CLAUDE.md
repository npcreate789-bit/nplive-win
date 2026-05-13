# CLAUDE.md — vcam-pc

Guidance for Claude Code (claude.ai/code) when working in this directory.

This file scopes the **vcam-pc** subproject (desktop streamer + dashboard). For the wider repo see `../README.md`. The repo's top-level `CLAUDE.md` (if present) describes an unrelated React starter — **ignore it when working under `vcam-pc/`**.

---

## What this project is

PC-side companion for vcam (virtual camera over TikTok Live). Three responsibilities:

1. **Streamer** — loop `videos/*.mp4` → FFmpeg encode → push over TCP/RTMP to phone
2. **Dashboard** — Tkinter GUI managing devices, licenses, hook status, auto-update
3. **LSPatch orchestration** — pull TikTok APKs from phone, inject vcam Xposed module, install back

Companion projects in the same repo:
- `../vcam-app/` — Android Xposed module (Kotlin, embedded into TikTok by LSPatch)
- `../vcam-server/` — license + announcements server (FastAPI on Docker)
- `../vcam-magisk/` — Magisk module variant of vcam-app

---

## Common commands

```bash
# Dev
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main --gui          # launch dashboard

# Tests (no phone / no ffmpeg needed; ~26 unit tests)
pytest -v
pytest tests/test_auto_update*.py tests/test_update_prefs.py -v   # update system
pytest tests/test_lspatch_*.py tests/test_hook_*.py -v            # patch pipeline

# Release (admin only — requires .private_key)
python3 tools/publish_update.py --notes "..."                     # cuts patch + signs manifest
python3 tools/build_pyinstaller.py                                # frozen .exe / .app
bash tools/build_macos_pkg.sh                                     # native .pkg installer
```

---

## Architecture map

### Entry point + UI

| File | Role |
|---|---|
| `src/main.py` | Entry point; CLI arg parsing, kicks off Tk app |
| `src/ui/studio_app.py` | Root Tk window, spawns background threads (UpdatePoller, DevicePoller, …) |
| `src/ui/studio_pages.py` | Dashboard, Settings, Wizard pages |
| `src/branding.py` | **Single source of truth for `BRAND.version`** (see Version Labeling below) |
| `src/config.py` | Loads/saves `config.json` (device profile, ffmpeg path, etc.) |

### Streaming + hook

| File | Role |
|---|---|
| `src/encode_push_runner.py`, `encode_push_tasks.py` | Per-device ffmpeg encode + push loop |
| `src/ffmpeg_streamer.py` | Low-level ffmpeg wrapper |
| `src/tcp_server.py` | TCP H.264 push (legacy path) |
| `src/rtmp_server.py` | RTMP push (preferred path) |
| `src/hook_mode.py` | Encode + push MP4 to `/sdcard/Android/data/<pkg>/files/vcam_final.mp4` |
| `src/scrcpy_mirror.py`, `scrcpy_installer.py` | Device screen mirror |

### Update + patch (the heart of this CLAUDE.md)

| File | Role |
|---|---|
| `src/auto_update.py` | **PC self-update**: manifest fetch, Ed25519 sig verify, SHA256 check, atomic swap, rollback |
| `src/update_prefs.py` | Persistent prefs: `install_on_close`, `auto_prefetch`, `last_check_ts` |
| `src/lspatch_pipeline.py` | **TikTok patching**: probe → pull APKs → LSPatch inject → install → rollback |
| `src/hook_status.py` | Probe whether TikTok on device is patched (signature fingerprint check) |
| `src/_ed25519.py`, `_pubkey.py` | Pure-Python Ed25519 verify; pubkey embedded for offline sig check |
| `src/announcements.py` | Same trust chain — fetches signed admin announcements |
| `tools/publish_update.py` | Admin tool: pack `src/`, sign manifest, write to `dist/updates/` |

### Adb + device management

| File | Role |
|---|---|
| `src/adb.py` | adb subprocess wrapper with timeouts |
| `src/wifi_adb.py` | Wireless ADB pair / connect |
| `src/customer_devices.py` | Per-device entries cached at `cache/devices.json` |
| `src/platform_tools.py` | Locate `adb` / `ffmpeg` / `lspatch.jar` across macOS/Win/Linux |

### License

| File | Role |
|---|---|
| `src/license_key.py` | Verify license (Ed25519 sig over machine_id + expiry) |
| `src/license_server.py` | HTTP client to `vcam-server` |
| `src/license_history.py` | Local ledger at `license_history.json` |

---

## Two update systems — keep these separate

This codebase has **two distinct "update" flows** that are commonly confused. Always clarify which one you're working on.

### 1. PC self-update (`auto_update.py`)

Updates the **vcam-pc desktop app itself** by replacing `src/`.

- **Discovery**: `UpdatePoller` polls `https://npcreate.github.io/updates/manifest.json` every 6h (overridable via `NP_UPDATE_MANIFEST_URL` env var)
- **Trust**: Ed25519 signature on manifest envelope, SHA256 on patch ZIP. Same keypair as licenses + announcements.
- **Apply**: extract ZIP → sibling `src.new/` → atomic `mv src/ → src.bak/` → `mv src.new/ → src/` → relaunch
- **Rollback**: `src.bak/` left until next clean boot
- **Where it's wired into UI**: Dashboard banner + Settings → "อัปเดตโปรแกรม" card

### 2. LSPatch TikTok patching (`lspatch_pipeline.py`)

Patches the **TikTok APK on a connected Android device** to inject vcam.

- **Flow**: probe tools → detect TikTok variant → `adb pull` base+splits → `java -jar lspatch.jar -m vcam-app.apk -l 2` → `adb uninstall` original → `adb install-multiple` patched
- **Rollback**: if install fails after uninstall, re-install the original APK set
- **Verify**: `hook_status.probe()` reads `dumpsys package` and looks for LSPatch debug-keystore fingerprint `e0b8d3e5`
- **Where it's wired into UI**: Wizard → "USB + Patch" mode, Dashboard "Patch" button

**Confusion to avoid**: a user asking to "patch 1.8.14" almost always means system 1 (PC self-update), not system 2 (TikTok injection).

---

## Version labeling — IMPORTANT non-obvious pattern

`BRAND.version` in `src/branding.py` is **intentionally decoupled** from actual code maturity.

- As of 2026-05-12: label says `"1.8.5"` but code is at **v1.8.13** maturity (Pack A: manual check-now, prefetch, resume, install-on-close).
- The revert is commit `8975380` ("brand: revert version label 1.8.13 → 1.8.5"). All v1.8.6 → v1.8.13 code remains at HEAD.
- Git tag `v1.8.13` on commit `1b479b0` preserves the real history.

**Implications:**
- Next legitimate patch version is **1.8.14**, NOT 1.8.6.
- Do NOT "fix" the label back to 1.8.13 without explicit user request — the decoupling powers the Pack A end-to-end test (a wide 1.8.5 → 1.8.14 gap stress-tests the min_compat gate and prefetch cache).
- When editing changelog / release notes, reference real code lineage, not the cosmetic label.

---

## Testing the auto-update flow end-to-end

Use the **localhost test rig** rather than real GitHub Pages — never push test artifacts to the live fleet.

```bash
# 1. Start local HTTP server in front of dist/updates/
cd vcam-pc/dist/updates && python3 -m http.server 8000 &

# 2. Run the app with overridden manifest URL
cd vcam-pc
NP_UPDATE_MANIFEST_URL=http://localhost:8000/manifest.json python3 -m src.main
```

Headless check (no GUI needed):
```bash
NP_UPDATE_MANIFEST_URL=http://localhost:8000/manifest.json python3 -c "
from src import auto_update
m = auto_update.fetch_manifest()
print(m.version, m.kind, m.min_compat_version)
p = auto_update.download_patch(m)   # hits cache if already prefetched
print('zip:', p)
"
```

**Required precondition**: `dist/updates/manifest.json` must be signed with `.private_key`. If you regenerate the patch ZIP, you MUST recompute SHA256 and re-sign the manifest, OR `auto_update.fetch_manifest()` returns `None` silently. The prefetch cache filename embeds the first 12 hex of SHA256 (`cache/updates/npcreate-src-X.Y.Z-{sha12}.zip`).

---

## Release workflow (admin only)

```bash
# Pre-flight
git status                                          # clean working tree
ls -la .private_key                                 # must exist, 32 bytes hex

# Bump version IN-TREE (if changing actual code lineage)
$EDITOR src/branding.py                             # bump BRAND.version

# Cut + sign
python3 tools/publish_update.py \
    --notes "ภาษาไทย changelog" \
    --min-compat 1.8.5

# Output
ls dist/updates/                                    # manifest.json + npcreate-src-X.Y.Z.zip

# Publish to GitHub Pages
cp dist/updates/manifest.json  /path/to/npcreate.github.io/updates/
cp dist/updates/npcreate-src-*.zip  /path/to/npcreate.github.io/updates/
cd /path/to/npcreate.github.io && git commit -am "v X.Y.Z" && git push
```

Safety: `publish_update.py` refuses to overwrite a manifest at a higher version unless `--allow-downgrade`.

---

## Constraints — do NOT change without discussion

- **`src/_pubkey.py`** — embedded in customer builds. Changing it invalidates every license, announcement, and pending update for existing customers. Treat as immutable.
- **`.private_key`** — never commit, never log, never send. Loss = no more signed updates ever.
- **Atomic swap in `apply_patch`** — `os.rename` across filesystems is NOT atomic; staging must live as sibling of `src/`, not in `/tmp/`. Don't "simplify" by moving to tempdir.
- **Manifest signature scheme** — `{format_version, payload (base64url JSON), signature (hex)}`. Don't add fields outside the signed payload, downstream verification trusts only `payload`.
- **`min_compat_version` default** = `<major>.<minor>.0`. Don't lower this casually — it's the safety net against old installs pulling incompatible patches.
- **Don't introduce async/asyncio** — the codebase uses threads + Tk `after()` consistently. Mixing event loops adds complexity for no win.

---

## Patterns to follow

- **Logging**: `log = logging.getLogger(__name__)` at module top. Don't `print()` (output is swallowed in frozen builds).
- **Errors**: define module-level exception class (`UpdateError`, `PatchError`, …) and raise it. Catch broad exceptions only at thread / UI boundaries.
- **UI callbacks**: bounce to Tk main thread via `self.after(0, fn)`. Calling Tk widgets from background threads is undefined behaviour.
- **Subprocess**: always pass timeout. Use `subprocess.run(..., timeout=N)`, never bare `.wait()`.
- **Progress callbacks**: accept `Optional[Callable[[int, int], None]]` (current, total). Always check `cancel_event` between chunks in long loops.
- **Paths**: `pathlib.Path` everywhere. Resolve to absolute early (`Path(...).resolve()`).
- **Config**: read at app start via `src/config.py`, pass values explicitly. Don't re-read `config.json` mid-flight.

---

## Where things live (filesystem)

| Path | Contents |
|---|---|
| `cache/updates/` | Prefetched patch ZIPs (filename embeds sha12) |
| `cache/update_prefs.json` | UpdatePrefs persisted state |
| `cache/devices.json` | Per-device records (patched_at, signature, tiktok_version) |
| `.cache/lspatch/pulled/` | TikTok APKs pulled from device |
| `.cache/lspatch/patched/` | LSPatch output APKs |
| `dist/updates/` | Built patch ZIPs + signed manifest (admin only) |
| `dist/pyinstaller/` | Frozen app bundles |
| `dist/installer/` | Native .pkg / .exe installers |
| `videos/` | Source MP4s loaded by streamer |
| `device_profiles.json` | Per-phone-model encoding presets |
| `license_history.json` | Local license ledger |
| `npcreate_dashboard.sqlite3` | Local dashboard state |

---

## Known fragility

- **Python 3.13 overlapped-entry zip rejection** breaks double-patched TikTok APKs — `lspatch_pipeline.py` falls back to `unzip -p` CLI
- **Thai locale Java crash** — `apkzlib` errors on Buddhist calendar year 2569 → subprocess env forces English locale
- **macOS Gatekeeper** — `.app` bundles need `xattr -dr com.apple.quarantine` after fresh download or they refuse to launch
- **Windows .exe with embedded ffmpeg/adb** — `tools/bin/` must be co-located with the .exe; relative path resolution in frozen builds is finicky (see `src/platform_tools.py`)
- **`UpdatePoller` 30s startup delay** — Pack A added `kick()` to wake it on demand, but the initial 30s pause is still there. Tests use `poll_now()` directly.
- **Python GC + Tk thread deadlock (commit `1a9a30e`)** — auto-GC running on a worker thread can finalise a Tk-owning object; the `__del__` routes via `Tkapp_ThreadSend` and blocks the worker until the main thread services it. If the main thread is meanwhile inside an `after()` callback waiting on a `threading.Lock`, you get a permanent idle freeze in minutes. Fix lives in `studio_app.py:__init__` — `gc.disable()` plus a 5 s `_gc_tick()` on the Tk thread. Do NOT re-enable auto-GC without re-introducing this hazard. Diagnosis recipe: `sample <pid> 3 -file /tmp/freeze.txt` and look for any background thread in `gc_collect_main → slot_tp_finalize → Tkapp_*` — that's the same bug recurring under a different finalizer.
- **vcam mode toggle needs BOTH broadcast AND sentinel file (commit `71f2104`)** — `CameraHook.resolvedMode()` in `../vcam-app/.../CameraHook.kt:230` falls back to `/data/local/tmp/vcam_enabled` whenever `currentMode == 0`. So a SET_MODE broadcast carrying `mode=0` flips the in-memory field, but the resolver still returns 2 if the on-disk flag exists, and the toggle silently no-ops on screen. Any PC code that flips vcam mode MUST keep the sentinel file in lock-step — touch it on Show, rm it on Hide. See `HookModePipeline.set_clip_visibility` for the canonical pattern. Also note: the receiver does NOT stop `VideoFeeder` for `mode != 2` (only `AudioFeeder`), so Show after a Hide needs `forceReload=true` to avoid resuming MediaPlayer at a stale mid-clip frame. Verification recipe: `adb shell ls /data/local/tmp/vcam_enabled` should match the intended state immediately after the toggle (file present = visible, absent = hidden).

---

## When uncertain

- Memory at `~/.claude/projects/-Users-npcreate-Downloads-livemobillrerun/memory/` has session-specific context (versioning decisions, test setup history)
- Recent design decisions: check `git log --oneline -n 20` — commit messages are descriptive
- Architecture deeper-dives: `docs/MANUAL_TH.md`, `docs/BUILD_INSTALLER.md`, `docs/SALES_KIT_TH.md`
- For Android-side questions, jump to `../vcam-app/`. For server-side, `../vcam-server/`.
