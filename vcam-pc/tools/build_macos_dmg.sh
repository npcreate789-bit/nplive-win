#!/usr/bin/env bash
# NP Create — macOS .dmg installer (v1.8.14).
#
# Drop-in replacement for the .pkg installer flow. We switched
# because macOS Installer.app's window chrome (Go Back / Continue
# buttons) was getting clipped on certain customer machines due to
# WebKit content-height quirks + non-standard DPI. .dmg drag-to-
# Applications avoids Installer.app entirely — same UX as OBS,
# Discord, Notion, etc.
#
# Build flow
# ----------
#
# 1. ``python tools/build_pyinstaller.py --clean`` produces
#    ``dist/pyinstaller/NP-Create.app`` (PyInstaller bundle).
# 2. This script copies ``.tools/macos/``, ``apk/`` and
#    ``MANUAL_TH.md`` INTO ``NP-Create.app/Contents/MacOS/`` so the
#    .app is fully self-contained — no sidecar folders need to
#    travel with it.
# 3. We stage ``NP-Create.app`` plus an ``Applications`` symlink
#    inside a clean folder, then ``hdiutil`` packages that folder
#    as a compressed read-only .dmg.
#
# Why hdiutil and not create-dmg
# ------------------------------
# ``hdiutil`` ships with every macOS install. ``create-dmg`` is a
# brew package that wraps hdiutil with a fancier DSL (background
# image, icon positioning, drag arrow). We sacrifice the drag arrow
# to gain "zero install dependencies on the build machine". The
# Applications symlink in the window is the universally-understood
# affordance — customers don't need an arrow to know what to do.
#
# Output
# ------
# ``dist/installer/NP-Create-<version>.dmg``
#
# Customer flow
# -------------
# 1. Download .dmg.
# 2. Double-click → volume window opens with NP-Create.app +
#    Applications shortcut side by side.
# 3. Drag NP-Create.app onto Applications.
# 4. Eject the .dmg.
# 5. Launch NP-Create from /Applications/ — done. No Python install
#    required; no installer wizard to walk through; nothing for
#    the OS to clip below the screen.

set -euo pipefail

# Force C locale before any tool invocation. hdiutil emits
# localised error strings on Thai-locale systems which break the
# few regexes downstream tools rely on; LC_ALL=C keeps everything
# in English.
export LC_ALL=C
export LANG=C

cd "$(dirname "$0")/.."
PROJECT="$(pwd)"
WORKSPACE="$(cd "$PROJECT/.." && pwd)"

# ── Admin / customer build flavour ──────────────────────────────
#
# An admin build embeds ``.private_key`` next to the executable so
# ``StudioApp.is_admin`` returns true and the "ออกคีย์ลูกค้า" button
# in the sidebar footer reveals AdminPage. The customer build OMITS
# the file — without it the property is False and the admin UI is
# completely invisible.
#
# Two ways to flip into admin mode:
#   1. ``bash tools/build_macos_dmg.sh --admin``
#   2. ``NPCREATE_ADMIN_BUILD=1 bash tools/build_macos_dmg.sh``
#
# Output filenames differ on purpose so admin and customer artifacts
# can never be confused for each other on disk.
BUILD_FLAVOR="customer"
if [[ "${NPCREATE_ADMIN_BUILD:-0}" == "1" ]]; then
    BUILD_FLAVOR="admin"
fi
for arg in "$@"; do
    if [[ "$arg" == "--admin" ]]; then
        BUILD_FLAVOR="admin"
    fi
done

VERSION="$(python3 -c 'import sys; sys.path.insert(0, "src"); from branding import BRAND; print(BRAND.version)')"
OUT_DIR="$PROJECT/dist/installer"
STAGING="$PROJECT/build/macos-dmg"
APP_SRC="$PROJECT/dist/pyinstaller/NP-Create.app"

if [[ "$BUILD_FLAVOR" == "admin" ]]; then
    DMG="$OUT_DIR/NP-Create-Admin-${VERSION}.dmg"
    VOL_NAME="NP Create Admin ${VERSION}"
else
    DMG="$OUT_DIR/NP-Create-${VERSION}.dmg"
    VOL_NAME="NP Create ${VERSION}"
fi

echo
echo " =============================================================="
echo "  NP Create — macOS Installer (.dmg) Build"
echo "  version : ${VERSION}"
echo "  flavour : ${BUILD_FLAVOR}"
echo " =============================================================="

if [[ "$BUILD_FLAVOR" == "admin" ]]; then
    echo
    echo "  ⚠️  ADMIN BUILD — embeds .private_key signing seed"
    echo "      DO NOT share this .dmg with customers."
    echo
fi

# ── 0. Preflight ──────────────────────────────────────────────────
if [[ ! -d "$APP_SRC" ]]; then
    echo
    echo "[!] $APP_SRC not found."
    echo "    Run: python3 tools/build_pyinstaller.py --clean"
    exit 1
fi

# ── 1. Stage a clean copy of the .app and embed sidecars ─────────
echo
echo "[1/4] staging .app + embedding sidecars"

rm -rf "$STAGING"
mkdir -p "$STAGING/volume"
VOLUME_ROOT="$STAGING/volume"
APP_DST="$VOLUME_ROOT/NP-Create.app"

# rsync -a preserves perms/symlinks. We exclude runtime detritus
# (logs / cache / videos / __pycache__ / .DS_Store) so the .dmg is
# reproducible byte-for-byte regardless of how often admin tested
# the .app before packaging.
rsync -a \
    --exclude='Contents/MacOS/logs/' \
    --exclude='Contents/MacOS/cache/' \
    --exclude='Contents/MacOS/videos/' \
    --exclude='__pycache__/' \
    --exclude='.DS_Store' \
    "$APP_SRC/" "$APP_DST/"

# Embed .tools/macos/ → NP-Create.app/Contents/MacOS/.tools/macos/
# The dereference flag (-L) follows the jdk-21 / lspatch / platform-
# tools symlinks at workspace's .tools/macos/ so we ship their real
# contents (otherwise we'd ship dangling links the customer can't
# resolve).
TOOLS_SRC="$WORKSPACE/.tools/macos"
if [[ -d "$TOOLS_SRC" ]]; then
    mkdir -p "$APP_DST/Contents/MacOS/.tools/macos"
    rsync -aL \
        --exclude='__pycache__/' \
        --exclude='.DS_Store' \
        "$TOOLS_SRC/" "$APP_DST/Contents/MacOS/.tools/macos/"
else
    echo "      [!] $TOOLS_SRC missing — bundle won't have adb/JDK/lspatch."
fi

# Embed apk/ → NP-Create.app/Contents/MacOS/apk/
if [[ -f "$WORKSPACE/apk/vcam-app-release.apk" ]]; then
    mkdir -p "$APP_DST/Contents/MacOS/apk"
    cp "$WORKSPACE/apk/vcam-app-release.apk" \
       "$APP_DST/Contents/MacOS/apk/vcam-app-release.apk"
fi

# Admin-only: embed the Ed25519 ``.private_key`` signing seed.
# StudioApp.is_admin checks ``(PROJECT_ROOT / ".private_key").is_file()``
# which in frozen mode points at ``<.app>/Contents/MacOS/.private_key``
# — exactly the file we drop here. Mode 0600 so only the user who
# extracted the .dmg can read it; the install pipeline preserves
# the bit via ``cp --preserve=mode`` semantics on macOS (default).
if [[ "$BUILD_FLAVOR" == "admin" ]]; then
    if [[ -f "$PROJECT/.private_key" ]]; then
        cp "$PROJECT/.private_key" "$APP_DST/Contents/MacOS/.private_key"
        chmod 600 "$APP_DST/Contents/MacOS/.private_key"
        echo "      [admin] embedded .private_key"
    else
        echo
        echo "  ❌ --admin requested but $PROJECT/.private_key does not exist."
        echo "     Run tools/init_keys.py to generate a keypair first,"
        echo "     or copy the existing .private_key into vcam-pc/."
        exit 1
    fi
fi

# Customer manual lives next to the .app inside the .dmg so it's
# discoverable WITHOUT having to right-click into the bundle.
[[ -f "$PROJECT/docs/MANUAL_TH.md" ]] && \
    cp "$PROJECT/docs/MANUAL_TH.md" "$VOLUME_ROOT/MANUAL_TH.md"

# Applications symlink — the standard drag-to-install affordance
# (kept as a fallback for power users who prefer drag-drop).
ln -s /Applications "$VOLUME_ROOT/Applications"

# One-click installer .command. Customers who follow the "double
# click this first" sticker in the .dmg get an automated install
# that strips Gatekeeper quarantine — no scary "cannot verify
# malware" dialog on macOS 14+. The leading emoji + space sorts
# the file to the top of the volume window in Finder so it's the
# first thing the customer sees. Filename is in Thai to match the
# product language and so customers don't need to guess what
# language the installer expects.
INSTALL_CMD_SRC="$PROJECT/tools/installer-macos/install_to_applications.command"
INSTALL_CMD_DST="$VOLUME_ROOT/📦 ติดตั้งลง Applications.command"
if [[ -f "$INSTALL_CMD_SRC" ]]; then
    cp "$INSTALL_CMD_SRC" "$INSTALL_CMD_DST"
    chmod +x "$INSTALL_CMD_DST"
fi

# Ad-hoc codesign the .app and strip any quarantine xattrs that
# might have hitched a ride from the build machine. Ad-hoc
# signing (``--sign -``) doesn't pass full Notarization but it
# DOES give the bundle a valid signature so library validation
# stops nagging on launch, and reduces the dialog wording from
# "cannot verify malware" to the milder "downloaded from
# internet, open?" on stock macOS. Combined with the installer
# .command's xattr strip, the customer's launch is silent.
echo "      ad-hoc signing + cleaning xattrs"
xattr -cr "$APP_DST" 2>/dev/null || true
codesign --force --deep --sign - "$APP_DST" 2>/dev/null || true

APP_SIZE=$(du -sh "$APP_DST" | awk '{print $1}')
VOL_FILES=$(find "$VOLUME_ROOT" -type f | wc -l | tr -d ' ')
echo "      .app size: $APP_SIZE"
echo "      volume   : NP-Create.app + Applications symlink + MANUAL_TH.md ($VOL_FILES files)"

# ── 2. Build the .dmg ─────────────────────────────────────────────
echo
echo "[2/4] hdiutil → compressed .dmg"

mkdir -p "$OUT_DIR"
rm -f "$DMG"

# UDZO = compressed read-only. Best size / compatibility tradeoff;
# what every standard macOS .dmg ships as.
hdiutil create \
    -volname "$VOL_NAME" \
    -srcfolder "$VOLUME_ROOT" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG" >/dev/null

# ── 3. Apply volume icon (optional cosmetic) ──────────────────────
echo
echo "[3/4] applying volume icon"
if [[ -f "$PROJECT/assets/logo.icns" ]]; then
    # Attach R/W, drop the .VolumeIcon.icns, detach, reconvert. This
    # is the standard way to give a .dmg a custom icon WITHOUT
    # create-dmg. We swallow stderr because the SetFile step is
    # advisory — on machines without Xcode CLT it warns but the
    # .dmg still works.
    MOUNT_DIR=$(mktemp -d)
    hdiutil attach "$DMG" -nobrowse -mountpoint "$MOUNT_DIR" >/dev/null 2>&1 || true
    if [[ -d "$MOUNT_DIR" ]]; then
        cp "$PROJECT/assets/logo.icns" "$MOUNT_DIR/.VolumeIcon.icns" 2>/dev/null || true
        SetFile -c icnC "$MOUNT_DIR/.VolumeIcon.icns" 2>/dev/null || true
        SetFile -a C "$MOUNT_DIR" 2>/dev/null || true
        hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
    fi
    echo "      icon set from assets/logo.icns"
else
    echo "      [-] assets/logo.icns missing — using default Finder icon"
fi

# ── 4. Done ───────────────────────────────────────────────────────
echo
echo "[4/4] verifying"
hdiutil verify "$DMG" >/dev/null
SIZE=$(du -h "$DMG" | awk '{print $1}')

echo
echo " =============================================================="
echo "  ✓ DONE"
echo " =============================================================="
echo "  Output: $DMG"
echo "  Size:   $SIZE"
echo
echo "  Customer flow:"
echo "    1. Double-click NP-Create-${VERSION}.dmg"
echo "    2. Drag NP-Create.app onto the Applications shortcut"
echo "    3. Eject the disk image"
echo "    4. Launch NP-Create from /Applications/"
echo "       (no Python install required — the .app self-contains)"
echo
