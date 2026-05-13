#!/bin/bash
# NP Create — one-click installer.
#
# Customer flow: mount .dmg → double-click this file → Terminal
# opens → script copies NP-Create.app into /Applications/, strips
# the Gatekeeper quarantine attribute, launches the app, and
# ejects the disk image. The customer never sees a "cannot verify
# malware" dialog because by the time macOS goes to launch the
# app, the quarantine xattr is already gone.
#
# Without this script the customer would have to:
#   1. Drag NP-Create.app to Applications
#   2. Double-click — see Gatekeeper warning ("downloaded from
#      internet, are you sure?" on macOS 11-13, or the harsher
#      "cannot verify developer" / "Apple cannot check it for
#      malware" on macOS 14+)
#   3. Right-click → Open, or visit System Settings → Privacy &
#      Security → Open Anyway
#
# That two-extra-clicks workflow is what triggered the support
# question. This script collapses it to a single double-click.

set -e

# Stay in the directory we were launched from — Terminal's CWD
# differs from where the .command file lives.
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_SRC="$HERE/NP-Create.app"
APP_DST="/Applications/NP-Create.app"

clear
echo
echo "  ════════════════════════════════════════════════════════════"
echo "    NP Create — ติดตั้งโปรแกรม"
echo "  ════════════════════════════════════════════════════════════"
echo

if [[ ! -d "$APP_SRC" ]]; then
    echo "  ❌ ไม่พบไฟล์ NP-Create.app ที่ตำแหน่งนี้:"
    echo "       $APP_SRC"
    echo
    echo "     กรุณาเปิดไฟล์ .dmg ใหม่อีกครั้ง"
    echo
    read -n 1 -p "  กด Enter เพื่อปิด..."
    exit 1
fi

# Already-installed handling — overwrite cleanly so update flows
# (customer reinstalls a newer .dmg over an existing copy) work
# without leaving stale Contents/Frameworks/* libs from the old
# version.
if [[ -d "$APP_DST" ]]; then
    echo "  ♻️  พบโปรแกรมเดิม — จะอัปเดตเป็นเวอร์ชันใหม่"
    echo
    # rm without sudo. If /Applications/ isn't writable by the
    # current user we let the error propagate so they see a clear
    # message rather than a half-installed app.
    if ! rm -rf "$APP_DST" 2>/dev/null; then
        echo "  ❌ ไม่สามารถลบโปรแกรมเดิมได้ — อาจต้องใช้รหัสผ่านผู้ดูแลระบบ"
        echo "     ลองปิด NP-Create ที่กำลังเปิดอยู่แล้วเปิดไฟล์นี้ใหม่"
        echo
        read -n 1 -p "  กด Enter เพื่อปิด..."
        exit 1
    fi
fi

echo "  📦 กำลังคัดลอกโปรแกรมไปยัง /Applications/ ..."
if ! cp -R "$APP_SRC" "$APP_DST" 2>/dev/null; then
    echo
    echo "  ❌ คัดลอกไม่สำเร็จ — โฟลเดอร์ /Applications/ อาจถูกป้องกัน"
    echo "     ลองลาก NP-Create.app ลง /Applications/ ใน Finder แทน"
    echo
    read -n 1 -p "  กด Enter เพื่อปิด..."
    exit 1
fi

# The xattr trick: macOS attaches com.apple.quarantine to files
# downloaded from the internet. The Gatekeeper warning fires off
# that attribute, so removing it makes the launch silent.
# ``-r`` walks the entire bundle (Info.plist, frameworks,
# helpers — Gatekeeper checks every binary).
echo "  🔓 ปลดสถานะ Gatekeeper ..."
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true

# Re-anchor any broken signatures created by the copy. ad-hoc
# resign is harmless if already signed and rescues edge cases
# where macOS treats the copied bundle as "modified".
codesign --force --deep --sign - "$APP_DST" 2>/dev/null || true

echo
echo "  ✓ ติดตั้งสำเร็จ"
echo
echo "  🚀 กำลังเปิดโปรแกรม..."
open "$APP_DST"

# Auto-eject the .dmg so the customer's Finder isn't cluttered
# with the mounted image after install. ``df`` resolves which
# /Volumes/... we came from; we detach in a backgrounded
# subshell so the script can exit cleanly before the unmount
# completes (unmounting our own CWD would otherwise stall).
DMG_VOLUME="$(df "$HERE" 2>/dev/null | tail -1 | awk '{$1=$2=$3=$4=$5=$6=$7=$8=""; sub(/^[ \t]+/, ""); print}')"
if [[ "$DMG_VOLUME" == /Volumes/* ]]; then
    (sleep 2 && hdiutil detach "$DMG_VOLUME" -force >/dev/null 2>&1) &
fi

echo
echo "  💡 แผ่นภาพดิสก์ (.dmg) จะถูกออกให้อัตโนมัติในอีกครู่"
echo "     ปิดหน้าต่าง Terminal นี้ได้เลยครับ"
echo
sleep 3
