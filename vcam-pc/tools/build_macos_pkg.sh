#!/usr/bin/env bash
# NP Create — macOS .pkg installer builder (v1.8.14, v2).
#
# Produces a SINGLE double-clickable .pkg file that installs
# NP-Create.app (PyInstaller bundle, Python embedded) plus its
# sibling .tools/macos + apk into /Applications/NP Create/. No
# external Python install required — the .app self-contains its
# own runtime.
#
# Modern Installer.app UX: branded background image, Thai welcome /
# license / conclusion screens, no per-component checkboxes.
#
# Usage:
#   1. python tools/build_pyinstaller.py --clean   (builds NP-Create.app)
#   2. bash tools/build_macos_pkg.sh
#
# Output:
#   dist/installer/NP-Create-Setup-<version>.pkg
#
# Prerequisites (all built into macOS — no Homebrew):
#   • pkgbuild + productbuild (ship with Xcode Command Line Tools)
#   • python3 (for BRAND.version extraction)
#
# Why two-stage pkgbuild + productbuild
# --------------------------------------
# ``pkgbuild`` produces a flat component pkg from a payload directory.
# ``productbuild`` wraps it with a Distribution XML for the
# Installer.app UI (welcome / license / conclusion / background
# image). The flat component pkg alone installs fine but skips
# straight to "Install" — no branding.

set -euo pipefail

export LC_ALL=C
export LANG=C

cd "$(dirname "$0")/.."
PROJECT="$(pwd)"
WORKSPACE="$(cd "$PROJECT/.." && pwd)"

OUT_DIR="$PROJECT/dist/installer"
STAGING="$PROJECT/build/macos-pkg"
RES_SRC="$PROJECT/tools/installer-macos/Resources"
SCRIPTS_SRC="$PROJECT/tools/installer-macos/scripts"
DIST_XML_SRC="$PROJECT/tools/installer-macos/distribution.xml"

VERSION="$(python3 -c 'import sys; sys.path.insert(0, "src"); from branding import BRAND; print(BRAND.version)')"
OUTPUT_PKG="$OUT_DIR/NP-Create-Setup-${VERSION}.pkg"

echo
echo " =============================================================="
echo "  NP Create — macOS Installer (.pkg) Build"
echo "  version : ${VERSION}"
echo " =============================================================="

# ── 1. Stage the payload ──────────────────────────────────────────
echo
echo "[1/4] staging payload"

# NP-Create.app — the PyInstaller bundle. Hard requirement.
APP_SRC="$PROJECT/dist/pyinstaller/NP-Create.app"
if [[ ! -d "$APP_SRC" ]]; then
    echo
    echo "[!] $APP_SRC not found."
    echo "    Run: python tools/build_pyinstaller.py --clean"
    exit 1
fi

rm -rf "$STAGING"
mkdir -p "$STAGING/payload"
APP_ROOT="$STAGING/payload"

# Layout after install (under /Applications/NP Create/):
#   NP-Create.app/   PyInstaller bundle — the entry point
#   .tools/macos/    adb, JDK 21, lspatch, ffmpeg, scrcpy, mediamtx
#   apk/             vcam-app-release.apk (LSPatch module)
#   MANUAL_TH.md     customer manual
#
# This is the same shape platform_tools._tools_root_base walks to
# find ``.tools/`` and ``apk/`` next to the .app — the 7-level cap
# (commit 0600684) covers this exact case.

# Copy the .app. Exclude runtime detritus from past local launches
# (logs / cache / videos / __pycache__ / .DS_Store) so the .pkg is
# reproducible byte-for-byte regardless of how often admin tested
# the .app before packaging.
rsync -a \
    --exclude='Contents/MacOS/logs/' \
    --exclude='Contents/MacOS/cache/' \
    --exclude='Contents/MacOS/videos/' \
    --exclude='__pycache__/' \
    --exclude='.DS_Store' \
    "$APP_SRC" "$APP_ROOT/"

# .tools/macos/ — rsync -L follows the JDK / lspatch / platform-
# tools symlinks at workspace's .tools/macos/ to copy their real
# contents (otherwise we'd ship dangling links).
TOOLS_SRC="$WORKSPACE/.tools/macos"
if [[ -d "$TOOLS_SRC" ]]; then
    mkdir -p "$APP_ROOT/.tools/macos"
    rsync -aL \
        --exclude='__pycache__/' \
        --exclude='.DS_Store' \
        "$TOOLS_SRC/" "$APP_ROOT/.tools/macos/"
else
    echo "      [!] $TOOLS_SRC missing — bundle won't have adb/JDK/lspatch."
fi

# apk/ — the vcam-app Xposed module that LSPatch fuses into TikTok.
if [[ -f "$WORKSPACE/apk/vcam-app-release.apk" ]]; then
    mkdir -p "$APP_ROOT/apk"
    cp "$WORKSPACE/apk/vcam-app-release.apk" "$APP_ROOT/apk/vcam-app-release.apk"
fi

# Customer manual.
[[ -f "$PROJECT/docs/MANUAL_TH.md" ]] && cp "$PROJECT/docs/MANUAL_TH.md" "$APP_ROOT/MANUAL_TH.md"

PAYLOAD_SIZE=$(du -sh "$APP_ROOT" | awk '{print $1}')
PAYLOAD_FILES=$(find "$APP_ROOT" -type f | wc -l | tr -d ' ')
echo "      payload: $PAYLOAD_SIZE / $PAYLOAD_FILES files"
echo "      layout : NP-Create.app + .tools/macos + apk + MANUAL_TH.md"

# ── 2. Build the NP Create component .pkg ─────────────────────────
echo
echo "[2/4] pkgbuild → component pkg"
NPCREATE_COMP_PKG="$STAGING/npcreate-component.pkg"

# CRITICAL: pkgbuild auto-discovers bundle directories (.app /
# .framework / jdk-21 / etc.) inside ``--root`` and marks every
# bundle "BundleIsRelocatable=YES" by default. macOS Installer then
# RELOCATES those bundles away from --install-location if it finds
# any same-CFBundleIdentifier copy elsewhere on disk (or even
# silently drops them if no relocate target exists), leaving the
# customer with a /Applications/NP Create/ that contains only the
# non-bundle siblings — exactly the apk/ + MANUAL_TH.md symptom
# we saw on first ship. The fix is a two-step build:
#
#   1. ``--analyze`` produces a component-plist with one entry per
#      detected bundle (NP-Create.app, Python.framework inside it,
#      .tools/macos/jdk-21, etc.).
#   2. Flip every BundleIsRelocatable to false so the installer
#      keeps each bundle at its --install-location-relative path.
#   3. Re-run pkgbuild with --component-plist so it honours the
#      edit instead of re-running --analyze.
COMP_PLIST="$STAGING/components.plist"
pkgbuild --analyze --root "$APP_ROOT" "$COMP_PLIST"

python3 - "$COMP_PLIST" <<'PY'
import plistlib, sys
p = sys.argv[1]
with open(p, "rb") as f:
    data = plistlib.load(f)
# components plist is a list of dicts, one per discovered bundle.
for entry in data:
    if isinstance(entry, dict):
        entry["BundleIsRelocatable"] = False
        # Also clear BundleHasStrictIdentifier just in case — that
        # flag triggers an extra "is this the same bundle?" check
        # that can also relocate.
        entry["BundleHasStrictIdentifier"] = False
with open(p, "wb") as f:
    plistlib.dump(data, f)
print(f"[ok] disabled BundleIsRelocatable on {len(data)} bundle entries")
PY

pkgbuild \
    --identifier "com.npcreate.studio" \
    --version "$VERSION" \
    --install-location "/Library/NP-Create-Stage" \
    --component-plist "$COMP_PLIST" \
    --scripts "$SCRIPTS_SRC" \
    --root "$APP_ROOT" \
    "$NPCREATE_COMP_PKG"

# ── 3. Render distribution.xml ────────────────────────────────────
echo
echo "[3/4] rendering distribution.xml"
DIST_XML="$STAGING/distribution.xml"
sed \
    -e "s|{NPC_TITLE}|NP Create ${VERSION}|g" \
    -e "s|{NPC_VERSION}|${VERSION}|g" \
    -e "s|{NPC_APP_PKG}|$(basename "$NPCREATE_COMP_PKG")|g" \
    "$DIST_XML_SRC" > "$DIST_XML"

# Copy Resources/ into the staging tree so productbuild can pick
# up welcome.html / license.txt / conclusion.html / background.png.
RES_STAGING="$STAGING/resources"
mkdir -p "$RES_STAGING"
cp -R "$RES_SRC"/* "$RES_STAGING/"

# ── 4. productbuild — final wrapper installer ─────────────────────
echo
echo "[4/4] productbuild → ${OUTPUT_PKG##*/}"
mkdir -p "$OUT_DIR"
rm -f "$OUTPUT_PKG"
productbuild \
    --distribution "$DIST_XML" \
    --package-path "$STAGING" \
    --resources "$RES_STAGING" \
    "$OUTPUT_PKG"

OUTPUT_SIZE=$(du -h "$OUTPUT_PKG" | awk '{print $1}')
echo
echo " =============================================================="
echo "  ✓ DONE"
echo " =============================================================="
echo "  Output: $OUTPUT_PKG"
echo "  Size:   $OUTPUT_SIZE"
echo
echo "  Customer flow:"
echo "    1. Double-click NP-Create-Setup-${VERSION}.pkg"
echo "    2. macOS Installer.app opens — Welcome / License / Install"
echo "    3. Installs to /Applications/NP Create/"
echo "    4. Double-click /Applications/NP Create/NP-Create.app"
echo "       (no Python install required — the .app self-contains)"
echo
