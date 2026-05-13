#!/usr/bin/env python3
"""NP Create — release bundler.

Produces ZIP archives for shipping to customers (or as an admin
backup) targeting macOS or Windows. The same source tree is used
for both audiences; the *only* differences are which auxiliary
files we copy in:

==================  ==========  ==========
File                Customer    Admin
==================  ==========  ==========
src/                yes         yes
src/_pubkey.py      yes         yes
src/_ed25519.py     yes         yes
.private_key        **NEVER**   yes
tools/init_keys.py  **NEVER**   yes
tools/gen_license.py **NEVER**  yes
tests/              no          yes
.tools/<os>/        yes         yes
apk/                yes         yes
run.bat / .command  yes         yes
README              yes         admin variant
==================  ==========  ==========

Usage::

    # Build a Windows zip for customers (default target)
    python tools/build_release.py --target customer --os windows

    # Build a macOS admin bundle
    python tools/build_release.py --target admin --os macos

    # Build all four combinations
    python tools/build_release.py --all

Exit codes: 0 success, 2 bad args, 3 prerequisite missing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent          # vcam-pc/
WORKSPACE = PROJECT.parent     # repo root

sys.path.insert(0, str(PROJECT))

from src.branding import BRAND  # noqa: E402

# ── what to ship ─────────────────────────────────────────────────

# Files inside vcam-pc/src/ never shipped — none currently. Listed
# here for completeness so future "secret" modules are caught.
SRC_BLOCKLIST: set[str] = set()

# Tool scripts we ship to customers (verify-only).
CUSTOMER_TOOLS: set[str] = set()

# Admin-only tools (signing, key rotation, packaging).
# Anything here is *removed* from customer bundles but kept in admin
# bundles. When you add a new helper script under ``tools/`` decide
# at write-time whether the customer needs it; if not, list it here.
ADMIN_TOOLS: set[str] = {
    # Python — admin signing, packaging, dev test
    "gen_license.py",
    "init_keys.py",
    "build_release.py",
    "build_pyinstaller.py",
    "_pyinstaller_entry.py",
    "_download_helper.py",
    "setup_windows_tools.py",
    "setup_macos_tools.py",
    "setup_ffmpeg.py",
    "setup_scrcpy.py",
    "setup_ci_tools.py",
    "fake_phone.py",
    "publish_announcement.py",
    # Auto-update publisher: signs and packages new patch ZIPs for
    # the in-app updater. Customers must NEVER receive this --
    # they'd be able to mint manifests... if they had the
    # ``.private_key`` (which they don't), but defence-in-depth
    # still says to keep the script itself off their disks.
    "publish_update.py",
    # Shell — dev environment bootstrap and smoke tests. Customers
    # don't need any of these (the launcher's pip install handles
    # runtime dependencies). Shipping them would just confuse a
    # non-technical customer who tries to "fix" things by running
    # them blindly.
    "bootstrap_macos.sh",
    "check_phone.sh",
    "install_jdk21.sh",
    "install_lspatch.sh",
    "install_python_macos.sh",
    "make_sample_video.sh",
    "phone_smoke.sh",
    "smoke_test.sh",
    "build_installer.bat",
    "build_dmg.sh",
    "build_macos_dmg.sh",
    "build_macos_pkg.sh",
    # Inno Setup script + EULA -- both are part of the *installer*
    # build pipeline (run on admin host), never shipped to customers.
    # Inside the installer the EULA gets renamed to LICENSE_TH.txt
    # and dropped into the install dir; the .iss source itself stays
    # private to the build pipeline.
    "installer.iss",
    "installer-license.txt",
}

# Top-level project files always shipped.
ALWAYS_SHIP_TOP = (
    "config.json",
    "device_profiles.json",
    "requirements.txt",
    "README.md",
)

# Customer-facing run launcher names. We auto-generate them so the
# ship payload doesn't depend on extra repo files.
LAUNCHER_NAMES = {
    "windows": "run.bat",
    "macos": "run.command",
    "linux": "run.sh",
}


# ── apk + tools sourcing ─────────────────────────────────────────


def find_vcam_apk() -> Path | None:
    """Locate the vcam-app APK to bundle. Prefer release > debug."""
    cands = [
        WORKSPACE / "apk" / "vcam-app-release.apk",
        WORKSPACE / "apk" / "vcam-app-debug.apk",
        WORKSPACE / "vcam-app/app/build/outputs/apk/release/app-release.apk",
        WORKSPACE / "vcam-app/app/build/outputs/apk/debug/app-debug.apk",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def find_tools_dir(os_name: str) -> Path | None:
    """Find the .tools/<os>/ payload, falling back to the legacy
    macOS-only flat layout if user hasn't reorganised yet."""
    per_os = WORKSPACE / ".tools" / os_name
    if per_os.is_dir():
        return per_os
    if os_name == "macos" and (WORKSPACE / ".tools").is_dir():
        return WORKSPACE / ".tools"
    return None


# Subset of the tools directory we actually ship. Anything outside
# these paths is build-time only (NDK, cmake, gradle, build-tools)
# and would balloon the customer ZIP from ~400 MB to >2 GB without
# adding any runtime value.
SHIP_TOOLS_PATTERNS = (
    "lspatch/",                         # lspatch.jar
    "jdk-21/",                          # JDK 21 (~330 MB)
    "platform-tools/",                  # adb + bundled libs (~38 MB)
    "android-sdk/platform-tools/",      # legacy macOS layout
    "ffmpeg",                           # static ffmpeg (file at root)
    "ffmpeg.exe",                       # Windows variant
    "scrcpy/",                          # screen mirror tool (~9 MB) —
                                        # populated by tools/setup_scrcpy.py;
                                        # without it customers fall back to
                                        # the in-app auto-installer (still
                                        # works, just needs internet on
                                        # first Mirror click).
    "adb-driver/",                      # Google USB Driver (~9 MB,
                                        # Windows only) — bundled INF
                                        # so the in-app help dialog
                                        # (WizardPage._show_driver_help)
                                        # can point Device Manager at
                                        # a known-good local folder
                                        # without an internet round-
                                        # trip. v1.7.11 fix for
                                        # "Mac เทสได้แต่ Windows ไม่ได้"
    "mediamtx/",                        # MediaMTX RTMP server (~26 MB)
                                        # — powers v1.8.0's "Mode B"
                                        # no-USB live path (PC →
                                        # MediaMTX ← phone's
                                        # CameraFi/Larix/DU Recorder
                                        # over WiFi; TikTok sees the
                                        # virtual camera). Ships on
                                        # both Windows and macOS so
                                        # customers who can't get
                                        # ADB drivers working still
                                        # have a path to live.
                                        # — Apple ships ADB-over-USB
                                        # support in the kernel,
                                        # Windows requires per-OEM
                                        # driver installs first.
)


def _under_pattern(rel: str, patterns: tuple[str, ...]) -> bool:
    rel_posix = rel.replace(os.sep, "/")
    return any(rel_posix.startswith(p) for p in patterns)


# ── zip writer ───────────────────────────────────────────────────


# ZIP file-mode constants (high 16 bits of ZipInfo.external_attr).
# 0o120000 = symlink, 0o100000 = regular file. The standard zipfile
# module doesn't expose these — we have to set them ourselves to get
# Finder, unzip(1), and Linux unzip to recreate the symlink at extract
# time instead of writing a tiny text file with the link target.
_ZIP_SYMLINK_MODE = 0o120755 << 16
_ZIP_FILE_MODE_EXEC = 0o100755 << 16
_ZIP_FILE_MODE = 0o100644 << 16


def _add_prebuilt_app(
    zf: "zipfile.ZipFile",
    prebuilt_dir: Path,
    prefix: str,
    os_name: str,
) -> int:
    """Pack the PyInstaller output into the customer zip.

    Two big gotchas live here:

    * macOS .app bundles contain *real* Unix symlinks (Python.framework
      indirections, dylib version aliases). The default
      ``ZipFile.write`` resolves them and writes the target file twice,
      which (a) bloats the zip and (b) once unzipped, dlopen finds two
      separate copies and crashes ("Failed to load Python shared
      library"). We fix this by detecting symlinks and writing them
      with the special 0o120000 mode bit.
    * The ``Contents/MacOS/<binary>`` entry must keep its executable
      bit; without it Finder will refuse to launch the app with a
      misleading "damaged" dialog.
    """
    import os

    n = 0
    # v1.8.13 leak fix: any developer who tested the .app locally
    # accumulates runtime files inside Contents/MacOS — logs from
    # _startup_diagnostic.py, update_prefs.json from the new
    # UpdatePrefs cache, npcreate.log, .DS_Store from Finder. Those
    # files leak into the customer ZIP if we naively walk the .app
    # tree. Skip the well-known runtime locations (same names the
    # repo-level _SHIP_SKIP_NAMES already excludes) so the shipped
    # bundle is reproducible byte-for-byte regardless of how often
    # the admin tested the .app before zipping.
    _APP_RUNTIME_SKIP = {
        "logs", "cache", "videos",
        "__pycache__", ".DS_Store", "Thumbs.db",
    }
    if os_name == "macos":
        app_dir = prebuilt_dir / "NP-Create.app"
        if not app_dir.is_dir():
            return 0
        for dirpath, dirnames, filenames in os.walk(app_dir, followlinks=False):
            # Prune runtime dirs early — directories rather than
            # individual files because logs/ holds many rotating
            # log files and listing each by name would miss new ones.
            dirnames[:] = [d for d in dirnames if d not in _APP_RUNTIME_SKIP]
            for fname in dirnames + filenames:
                if fname in _APP_RUNTIME_SKIP:
                    continue
                full = Path(dirpath) / fname
                rel = full.relative_to(prebuilt_dir).as_posix()
                # v1.8.13+: ZIP-root placement — ``<bundle>/NP-Create.app``
                # rather than the previous ``<bundle>/app/NP-Create.app``.
                # Dropping the ``app/`` subdirectory means customers see
                # the icon immediately on unzip (no need to descend into
                # a subfolder) AND it shortens the platform_tools walk by
                # one level (3 hops MacOS→Contents→.app→bundle vs 4),
                # which keeps the resolver well inside the 7-level cap.
                arcname = f"{prefix}/{rel}"
                if full.is_symlink():
                    target = os.readlink(full)
                    zinfo = zipfile.ZipInfo(arcname)
                    zinfo.create_system = 3  # 3 = Unix
                    zinfo.external_attr = _ZIP_SYMLINK_MODE
                    zf.writestr(zinfo, target)
                    n += 1
                elif full.is_file():
                    mode = full.stat().st_mode
                    is_exec = bool(mode & 0o111)
                    zinfo = zipfile.ZipInfo(arcname)
                    zinfo.create_system = 3
                    zinfo.external_attr = (
                        _ZIP_FILE_MODE_EXEC if is_exec else _ZIP_FILE_MODE
                    )
                    zinfo.compress_type = zipfile.ZIP_DEFLATED
                    with full.open("rb") as fh:
                        zf.writestr(zinfo, fh.read())
                    n += 1
    elif os_name == "windows":
        exe = prebuilt_dir / "NP-Create.exe"
        if exe.is_file():
            # v1.8.13+: ZIP-root placement (see macOS branch above for
            # the same rationale — icon visible immediately, shorter
            # platform_tools walk to find .tools/ next to the exe).
            zf.write(exe, f"{prefix}/NP-Create.exe")
            n += 1
    return n


def _walk_filtered(root: Path, skip_names: set[str]) -> list[Path]:
    """Iterate over files under ``root`` skipping any whose path
    contains one of ``skip_names`` (exact directory or file name).

    Symlinks are followed (``followlinks=True``) because the macOS
    setup script (``setup_macos_tools.py``) uses symlinks to point
    ``.tools/macos/jdk-21`` at the legacy ``.tools/jdk-21``. Without
    the follow flag the customer bundle would only contain the
    bare symlink — which doesn't survive a zip extract on the
    target machine and breaks the customer's first-run experience.
    """
    import os

    out: list[Path] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        # Prune directories early — both saves walk time and lets us
        # honour skip_names on directory boundaries.
        dirnames[:] = [d for d in dirnames if d not in skip_names]
        for fname in filenames:
            if fname in skip_names:
                continue
            p = Path(dirpath) / fname
            real = str(p.resolve())
            if real in seen:
                continue  # cycle protection
            seen.add(real)
            out.append(p)
    return out


# Names dropped from any ship — junk from tooling.
_SHIP_SKIP_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
    "Thumbs.db",
    ".gradle",
    ".idea",
    "build",     # gradle/pyinstaller artefacts inside subprojects
    "dist",
    "videos",    # user's working clips, not ours to ship
    "logs",
    "cache",     # vcam-pc/cache/ — runtime
    ".cache",
}


def add_dir(zf: zipfile.ZipFile, src_dir: Path, arc_dir: str,
            extra_skip: set[str] = frozenset()) -> int:
    n = 0
    for f in _walk_filtered(src_dir, _SHIP_SKIP_NAMES | set(extra_skip)):
        rel = f.relative_to(src_dir)
        zf.write(f, f"{arc_dir}/{rel.as_posix()}")
        n += 1
    return n


def add_file(zf: zipfile.ZipFile, src: Path, arc: str) -> None:
    zf.write(src, arc)


# ── launcher generation ──────────────────────────────────────────


def _launcher_body(os_name: str) -> str:
    """Generate the OS-native double-clickable launcher.

    Goals (in priority order):
    1. Customer with **zero technical background** can double-click
       and the program opens — no terminal interaction required.
    2. If something is missing (Python, pip), show a Thai-language
       error that tells them exactly what to do, in a window that
       does not auto-close.
    3. First-run pip install is silent on success and verbose on
       failure (so we can copy-paste an error into Line support).
    """
    if os_name == "windows":
        # cmd.exe parses batch files using the **OEM codepage**, not
        # whatever ``chcp`` is set to. If we put Thai bytes in here
        # the parser tries to execute the garbled tokens as commands
        # ("'\xe0\xb8\x82' is not recognized as an internal or
        # external command…"). So the .bat itself stays **strictly
        # ASCII**, and the moment Python is available we hand off all
        # Thai user messaging to a tiny Python entrypoint
        # (``src/_winlauncher.py``) — Python opens files as UTF-8
        # explicitly, so Thai prints fine in the same cmd window once
        # we ``chcp 65001`` it for the *output* side.
        #
        # Other cmd.exe quirks worth remembering:
        #   - %~dp0 = this script's dir, with trailing backslash.
        #   - ``call`` not ``start`` for pip so errorlevel propagates.
        #   - install log goes to %TEMP% so a customer who hits a
        #     pip failure can attach it to Line support.
        return textwrap.dedent("""\
            @echo off
            setlocal enableextensions
            chcp 65001 > nul
            title NP Create
            REM NP Create -- Windows launcher (ASCII-only by design)
            cd /d "%~dp0vcam-pc"

            set "PYBIN="
            where py >nul 2>&1 && set "PYBIN=py -3"
            if "%PYBIN%"=="" (
                where python >nul 2>&1 && set "PYBIN=python"
            )

            if "%PYBIN%"=="" (
                echo.
                echo  ==========================================================
                echo    [!] Python 3 is not installed on this PC
                echo  ==========================================================
                echo.
                echo    Steps to install Python:
                echo    1. Open https://www.python.org/downloads
                echo    2. Download Python 3.13 ^(Windows installer 64-bit^)
                echo    3. IMPORTANT: tick "Add Python 3.13 to PATH"
                echo    4. Finish the installer, then double-click run.bat again
                echo.
                echo    See INSTALL_TH.txt next to this file for Thai instructions.
                echo    Contact admin via Line OA: @npcreate
                echo.
                pause
                exit /b 1
            )

            REM Hand off to Python -- anything Thai/UTF-8 lives there,
            REM not in this batch file.
            %PYBIN% -X utf8 -m src._winlauncher
            set "RC=%ERRORLEVEL%"
            if not "%RC%"=="0" (
                echo.
                echo  [!] Launcher exited with code %RC%.
                echo      Send the message above to admin via Line OA.
                pause
            )
            exit /b %RC%
            """)
    if os_name == "macos":
        # macOS .command launchers always open a Terminal window —
        # we keep its output minimal and use osascript to show a
        # *real* dialog box for any error, since most non-tech users
        # will close the terminal panel without reading it.
        return textwrap.dedent("""\
            #!/usr/bin/env bash
            # NP Create — macOS launcher (v1.4)
            set -u

            cd "$(dirname "$0")/vcam-pc"

            err_dialog () {
                osascript -e "display dialog \\"$1\\" buttons {\\"OK\\"} default button 1 with icon stop with title \\"NP Create\\""
            }

            if ! command -v python3 >/dev/null 2>&1; then
                err_dialog "ต้องติดตั้ง Python 3 ก่อนใช้งานครับ\\n\\n1. เปิดเว็บ https://www.python.org/downloads\\n2. ดาวน์โหลด Python 3.13 สำหรับ macOS\\n3. ติดตั้งจนเสร็จ แล้วดับเบิ้ลคลิก run.command อีกครั้ง"
                exit 1
            fi

            echo
            echo "  NP Create — กำลังติดตั้งส่วนประกอบครั้งแรก (ใช้ Internet)"
            echo "  อย่าปิดหน้าต่างนี้ ใช้เวลาประมาณ 30 วินาที..."
            echo

            LOG="$TMPDIR/NP-Create-install.log"
            if ! python3 -m pip install --upgrade --quiet --user -r requirements.txt > "$LOG" 2>&1; then
                err_dialog "ติดตั้งส่วนประกอบไม่สำเร็จ\\nlog อยู่ที่ $LOG\\n\\nส่งไฟล์นี้ให้แอดมินทาง Line ได้เลยครับ"
                exit 1
            fi

            echo "  เปิดโปรแกรมแล้ว..."
            exec python3 -m src.main --studio
            """)
    # linux
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        # NP Create — Linux launcher (v1.4)
        set -e
        cd "$(dirname "$0")/vcam-pc"
        if ! command -v python3 >/dev/null 2>&1; then
            echo "[!] python3 not found — install with your package manager"
            exit 1
        fi
        python3 -m pip install --upgrade --quiet --user -r requirements.txt
        exec python3 -m src.main --studio
        """)


def _windows_install_thai() -> str:
    """Thai-language install instructions shipped alongside run.bat.

    The .bat must remain ASCII (cmd.exe parser uses the OEM
    codepage), so all Thai messaging for the *pre-Python* error
    path lives in this sibling text file. Customers double-click
    it from Explorer and it opens in Notepad as UTF-8.
    """
    return (
        "NP Create — คู่มือติดตั้ง (Windows)\n"
        "===================================\n"
        "\n"
        "ถ้าดับเบิ้ลคลิก run.bat แล้วขึ้นข้อความว่าหา Python ไม่เจอ\n"
        "(ขึ้น \"Python is not installed on this PC\")\n"
        "ให้ทำตามนี้ครับ:\n"
        "\n"
        "1. เปิดเว็บ https://www.python.org/downloads\n"
        "2. กด \"Download Python 3.13\" (Windows installer 64-bit)\n"
        "3. เปิดไฟล์ที่โหลดมา (.exe)\n"
        "4. **สำคัญ** — ติ๊กถูกที่ช่อง \"Add Python 3.13 to PATH\"\n"
        "   (อยู่ด้านล่างหน้าต่างติดตั้ง ก่อนกด Install Now)\n"
        "5. กด Install Now รอจนเสร็จ แล้วกด Close\n"
        "6. กลับมาดับเบิ้ลคลิก run.bat อีกครั้ง\n"
        "\n"
        "ครั้งแรกที่เปิด ระบบจะดาวน์โหลดส่วนประกอบ ~30 วินาที\n"
        "(ต้องต่อเน็ต) — อย่าปิดหน้าต่างจนกว่าโปรแกรมจะเปิดขึ้นมาเอง\n"
        "\n"
        "ถ้ายังเจอปัญหา ส่งข้อความใน Line OA: @npcreate ได้เลยครับ\n"
        "ทำงานทุกวัน 9:00-22:00 น.\n"
    )


# ── README generation ────────────────────────────────────────────


def _readme(target: str, os_name: str, *, has_prebuilt_app: bool = False) -> str:
    """Generate the customer-facing README inside the ZIP.

    ``has_prebuilt_app`` flips the install instructions: when the
    bundle ships a PyInstaller .app/.exe alongside the Python tree,
    we lead with the native binary (customer doesn't need Python
    installed at all) and demote ``run.bat`` / ``run.command`` to
    a "power-user fallback" footnote. When no prebuilt is present
    the README falls back to the legacy run.bat/.command flow that
    pre-dates v1.8.13 — bit-for-bit identical so admin can still
    ship a Python-only bundle if they need to skip the PyInstaller
    step for any reason.
    """
    is_admin = target == "admin"
    title = (
        f"{BRAND.name} — Admin Bundle"
        if is_admin
        else f"{BRAND.name} — Customer Bundle"
    )
    sec_admin = textwrap.dedent("""\
        ## ADMIN ONLY — สำหรับเจ้าของระบบเท่านั้น

        ห้ามแชร์ไฟล์ `.private_key` หรือ `tools/gen_license.py` ให้ใครเด็ดขาด
        - `.private_key` คือกุญแจส่วนตัวที่ใช้เซ็น license key ทุกใบที่ขายไป
        - หากหลุด ต้อง rotate กุญแจ (`python tools/init_keys.py --force`)
          ซึ่งจะทำให้ license key ทุกใบที่ขายไปแล้ว ใช้ไม่ได้ทันที

        ### ออกคีย์ให้ลูกค้า

        ```
        python tools/gen_license.py -c "ชื่อลูกค้า"            # ดีฟอลต์ 3 เครื่อง / 30 วัน
        python tools/gen_license.py -c "VIP" -n 5 -d 365      # 5 เครื่อง / 1 ปี
        ```

        ### Build customer bundle

        ```
        # Python-only bundle (smaller zip, customer needs Python 3.13):
        python tools/build_release.py --target customer --os windows
        python tools/build_release.py --target customer --os macos

        # With native binary (zero-dependency, ~80 MB larger zip):
        python tools/build_release.py --target customer --os macos --with-app
        ```
        ผลลัพธ์อยู่ใน `dist/`
        """)

    if has_prebuilt_app:
        # Lead with the native binary — no Python installation needed.
        # v1.8.13+ ship: the binary sits at the ZIP root
        # (``<bundle>/NP-Create.app`` or ``<bundle>/NP-Create.exe``)
        # rather than the previous ``app/`` subdirectory, so the
        # customer's first unzip view shows the icon immediately and
        # they don't have to know what "app" means.
        #
        # When the binary is shipped we OMIT the run.bat / run.command
        # launcher from the ZIP entirely (build_one skips writing it).
        # That keeps the customer bundle layout unambiguous — one
        # entry point, double-click and go. The README therefore
        # mentions ONLY the binary path with no "alternatively use
        # run.command" footnote (the launcher isn't in the ZIP for
        # them to find anyway).
        binary_name = (
            "NP-Create.exe" if os_name == "windows"
            else "NP-Create.app"
        )
        sec_install = textwrap.dedent(f"""\
            ## เริ่มต้นใช้งานเร็ว ๆ (Quick Start) ⚡

            **ไม่ต้องลง Python — ดับเบิ้ลคลิกใช้งานได้เลย**

            1. แตก zip นี้ไว้ที่ Desktop
            2. ดับเบิ้ลคลิก `{binary_name}` (ใช้งานได้ทันที)
            3. กรอก License Key → กด "เปิดใช้งาน"
            4. เสียบ USB มือถือ → กด "เพิ่มเครื่องใหม่" → ทำตาม Wizard

            > **อ่านคู่มือฉบับเต็ม** ใน `MANUAL_TH.md` (วิธีต่อ WiFi, แก้ปัญหา,
            > FAQ และอื่น ๆ ครบทุกข้อ)

            ## ติดต่อแอดมิน

            - Line OA: **{BRAND.line_oa}**
            - เวลาทำการ: {BRAND.support_hours}
            - **ส่งภาพหน้าจอ + ข้อความ error** จะแก้ให้เร็วขึ้น
            """)
    else:
        # Legacy Python-only path (pre-v1.8.13 ship layout). Kept
        # bit-for-bit identical so a fall-back ZIP without the
        # PyInstaller bundle still reads exactly like v1.8.12 did.
        launcher_step = (
            "ดับเบิ้ลคลิก `run.bat`"
            if os_name == "windows"
            else "ดับเบิ้ลคลิก `run.command`"
        )
        sec_install = textwrap.dedent(f"""\
            ## เริ่มต้นใช้งานเร็ว ๆ (Quick Start)

            1. ลง **Python 3.13** จาก https://www.python.org/downloads
               {'(สำคัญ: ติ๊ก "Add Python to PATH" ตอนลง)' if os_name == 'windows' else ''}
            2. แตก zip นี้ไว้ที่ Desktop
            3. {launcher_step}
            4. รอ ~30 วินาที (ติดตั้งส่วนประกอบครั้งแรก)
            5. กรอก License Key → กด "เปิดใช้งาน"
            6. เสียบ USB มือถือ → กด "เพิ่มเครื่องใหม่" → ทำตาม Wizard

            > **อ่านคู่มือฉบับเต็ม** ใน `MANUAL_TH.md` (วิธีต่อ WiFi, แก้ปัญหา,
            > FAQ และอื่น ๆ ครบทุกข้อ)

            ## ติดต่อแอดมิน

            - Line OA: **{BRAND.line_oa}**
            - เวลาทำการ: {BRAND.support_hours}
            - **ส่งภาพหน้าจอ + ข้อความ error** จะแก้ให้เร็วขึ้น
            """)

    sections = [
        f"# {title}\n\nเวอร์ชัน {BRAND.version}\n",
        sec_install,
    ]
    if is_admin:
        sections.append(sec_admin)
    return "\n".join(sections)


# ── build entrypoint ─────────────────────────────────────────────


def build_one(target: str, os_name: str, dist: Path) -> Path:
    if target not in ("customer", "admin"):
        raise ValueError(f"unknown target: {target}")
    if os_name not in ("windows", "macos", "linux"):
        raise ValueError(f"unknown os: {os_name}")

    # Required inputs.
    apk = find_vcam_apk()
    if not apk:
        raise SystemExit(
            "[!] vcam-app APK not found. Build it first:\n"
            "      cd vcam-app && ./gradlew assembleDebug"
        )
    tools_dir = find_tools_dir(os_name)
    if not tools_dir and os_name != "linux":
        print(
            f"[!] No bundled tools found for {os_name} at "
            f".tools/{os_name}/. The zip will lack adb/ffmpeg/JDK; "
            f"customer must install them manually.",
            file=sys.stderr,
        )

    # Public key is mandatory — that's what verifies licenses.
    pubkey = PROJECT / "src" / "_pubkey.py"
    if not pubkey.is_file():
        raise SystemExit(
            "[!] Public key missing. Run `python tools/init_keys.py` "
            "once on the admin machine first."
        )

    bundle_name = (
        f"{BRAND.short_name.replace(' ', '-')}-"
        f"{target}-{os_name}-{BRAND.version}"
    )
    out_zip = dist / f"{bundle_name}.zip"
    dist.mkdir(parents=True, exist_ok=True)

    # Determine which tools-subdir files are admin-only.
    tools_skip = set() if target == "admin" else ADMIN_TOOLS

    print(f"\n→ {target.upper()} bundle for {os_name}")
    print(f"   archive : {out_zip}")
    if tools_dir:
        print(f"   tools/  : {tools_dir}")
    print(f"   apk     : {apk}")

    with zipfile.ZipFile(
        out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        prefix = bundle_name

        # ── vcam-pc/src/ ─────────────────────────────────────────
        n_src = 0
        for f in _walk_filtered(PROJECT / "src", _SHIP_SKIP_NAMES):
            rel = f.relative_to(PROJECT / "src")
            if rel.name in SRC_BLOCKLIST:
                continue
            zf.write(f, f"{prefix}/vcam-pc/src/{rel.as_posix()}")
            n_src += 1
        print(f"   src/    : {n_src} files")

        # ── vcam-pc/tools/ ───────────────────────────────────────
        n_tools = 0
        for f in (PROJECT / "tools").glob("*"):
            if not f.is_file():
                continue
            if f.name in tools_skip:
                continue
            zf.write(f, f"{prefix}/vcam-pc/tools/{f.name}")
            n_tools += 1
        print(f"   tools/  : {n_tools} files (skipped {sorted(tools_skip)})")

        # ── vcam-pc top-level ───────────────────────────────────
        for fname in ALWAYS_SHIP_TOP:
            f = PROJECT / fname
            if f.is_file():
                zf.write(f, f"{prefix}/vcam-pc/{fname}")

        # ── vcam-pc/assets/ (logo + icon — needed by the UI) ────
        assets_dir = PROJECT / "assets"
        if assets_dir.is_dir():
            n_assets = add_dir(
                zf, assets_dir, f"{prefix}/vcam-pc/assets",
            )
            print(f"   assets/ : {n_assets} files (logo, icon)")

        # ── tests/ (admin only) ─────────────────────────────────
        if target == "admin":
            n_tests = add_dir(
                zf, PROJECT / "tests", f"{prefix}/vcam-pc/tests"
            )
            print(f"   tests/  : {n_tests} files")

        # ── private_key (admin only) ────────────────────────────
        priv = PROJECT / ".private_key"
        if target == "admin" and priv.is_file():
            zf.write(priv, f"{prefix}/vcam-pc/.private_key")
            print("   .private_key : included (ADMIN ONLY)")
        elif target == "customer" and priv.is_file():
            print("   .private_key : EXCLUDED (customer-safe)")

        # ── license_history.json (admin only) ───────────────────
        hist = PROJECT / "license_history.json"
        if target == "admin" and hist.is_file():
            zf.write(hist, f"{prefix}/vcam-pc/license_history.json")
            print("   license_history.json : included (ADMIN ONLY)")
        elif target == "customer" and hist.is_file():
            print("   license_history.json : EXCLUDED (customer-safe)")

        # ── apk ─────────────────────────────────────────────────
        # Ship as ``vcam-app-release.apk`` -- this name is the FIRST
        # entry in ``platform_tools.find_vcam_apk()``'s search list,
        # so the customer build picks it up immediately. We used to
        # ship as ``vcam-app.apk``; that left ``find_vcam_apk()``
        # returning None on the customer side because the search
        # list never included that bare name. Net effect: the patch
        # flow silently failed on Windows ("render ไม่ผ่าน adb")
        # because it couldn't locate the Xposed module APK to feed
        # into LSPatch. Renaming here keeps the build-time and
        # runtime conventions aligned.
        zf.write(apk, f"{prefix}/apk/vcam-app-release.apk")

        # ── tools (.tools/<os>/) ────────────────────────────────
        if tools_dir:
            # v1.8.13 fix: shipping silently-broken symlinks bug.
            # ``.tools/<os>/jdk-21`` is sometimes a symlink to the
            # legacy ``.tools/jdk-21/`` (when setup_macos_tools.py
            # was run against a previous home dir, e.g. an older
            # admin's user path). os.walk(followlinks=True) silently
            # skips broken symlinks → the resulting ZIP ships
            # ``.tools/macos/`` with ffmpeg + scrcpy + mediamtx
            # only, NO jdk-21 / lspatch / platform-tools. The
            # customer .app then crashes on first Patch click with
            # "Java 0 is too old" + "lspatch.jar missing" because
            # nothing in find_java / find_lspatch_jar's search
            # tree exists. Pre-flight check warns BEFORE we waste
            # 5 minutes packing a 290 MB ZIP that's broken.
            critical = {
                "jdk-21": tools_dir / "jdk-21",
                "lspatch": tools_dir / "lspatch",
                "platform-tools": tools_dir / "platform-tools",
            }
            broken_critical: list[str] = []
            for name, p in critical.items():
                # Symlink that doesn't resolve → broken. ``exists``
                # follows the link and reports the underlying target.
                if p.is_symlink() and not p.exists():
                    broken_critical.append(
                        f".tools/{os_name}/{name} → "
                        f"{os.readlink(p)} (target missing)"
                    )
            if broken_critical:
                print(
                    "   [!] BROKEN SYMLINKS detected — these would ship "
                    "as empty dirs:",
                    file=sys.stderr,
                )
                for line in broken_critical:
                    print(f"       • {line}", file=sys.stderr)
                print(
                    "       Re-run setup_macos_tools.py / setup_windows_tools.py "
                    "on THIS machine OR re-create the symlinks pointing at "
                    f"{WORKSPACE / '.tools'} on this user's account before "
                    "building. The customer .app would otherwise crash with "
                    "'Java 0 is too old' / 'lspatch.jar missing' on first Patch.",
                    file=sys.stderr,
                )

            n_t = 0
            for f in _walk_filtered(tools_dir, _SHIP_SKIP_NAMES):
                rel = f.relative_to(tools_dir).as_posix()
                if not _under_pattern(rel, SHIP_TOOLS_PATTERNS):
                    continue
                # Normalise legacy android-sdk/platform-tools/ →
                # platform-tools/ so the customer ship has a flat,
                # predictable layout regardless of where dev built it.
                if rel.startswith("android-sdk/"):
                    rel = rel[len("android-sdk/"):]
                zf.write(f, f"{prefix}/.tools/{os_name}/{rel}")
                n_t += 1
            extras = "adb + JDK + lspatch + ffmpeg + scrcpy + mediamtx"
            if os_name == "windows":
                extras += " + adb-driver"
            print(f"   .tools/ : {n_t} files ({extras})")
            mtx_bin = tools_dir / "mediamtx" / (
                "mediamtx.exe" if os_name == "windows" else "mediamtx"
            )
            if not mtx_bin.is_file():
                print(
                    "   [!] .tools/{}/mediamtx/ missing — Mode B (RTMP)\n"
                    "       wizard will surface 'mediamtx not found' to\n"
                    "       customers and they won't be able to use the\n"
                    "       no-USB live path. To bundle, run:\n"
                    "          python tools/setup_ci_tools.py --os {} --skip platform-tools --skip jdk --skip lspatch --skip ffmpeg --skip adb-driver"
                    .format(os_name, os_name),
                    file=sys.stderr,
                )
            scrcpy_dir = tools_dir / "scrcpy"
            if not scrcpy_dir.is_dir():
                print(
                    "   [!] .tools/scrcpy/ missing — customer Mirror will need\n"
                    "       to auto-download on first click. To bundle, run:\n"
                    f"          python tools/setup_scrcpy.py --os {os_name}",
                    file=sys.stderr,
                )
            if os_name == "windows":
                drv_inf = tools_dir / "adb-driver" / "usb_driver" / "android_winusb.inf"
                if not drv_inf.is_file():
                    print(
                        "   [!] .tools/windows/adb-driver/ missing — Xiaomi/Redmi\n"
                        "       customers will be stuck on \"Allow USB Debugging\"\n"
                        "       prompt without an OEM driver. To bundle, run:\n"
                        "          python tools/setup_ci_tools.py --os windows --skip platform-tools --skip jdk --skip lspatch --skip ffmpeg",
                        file=sys.stderr,
                    )

        # ── prebuilt .app/.exe (optional) ───────────────────────
        # If the admin ran `tools/build_pyinstaller.py` first, we
        # bundle the resulting native binary alongside the rest of
        # the customer ZIP. When the binary is present the customer
        # doesn't need Python at all and the launcher script
        # (run.bat / run.command) becomes dead weight — so v1.8.13+
        # SKIPS the launcher entirely whenever the binary for that
        # OS is in the bundle. Missing-binary ZIPs still get the
        # Python-only launcher + INSTALL_TH.txt + README so legacy
        # flows keep working unchanged.
        prebuilt_dir = PROJECT / "dist" / "pyinstaller"
        has_prebuilt_app = False
        if prebuilt_dir.is_dir():
            n_pre = _add_prebuilt_app(zf, prebuilt_dir, prefix, os_name)
            if n_pre:
                has_prebuilt_app = True
                print(f"   app/    : {n_pre} files (PyInstaller bundle)")
            elif os_name in ("macos", "windows"):
                # The dist/pyinstaller dir exists but doesn't carry
                # an artifact for this OS — common when the admin
                # built the .app on Mac and is now packing a Windows
                # ZIP (PyInstaller can't cross-build). Flag it so
                # the resulting ZIP's README + launcher copy reflects
                # the Python-only flow, not the misleading
                # "double-click NP-Create.exe" copy.
                print(
                    f"   app/    : (no {os_name} artifact in "
                    f"dist/pyinstaller — Python-only bundle)"
                )

        # ── launcher (only when no native binary present) ───────
        # We OMIT run.bat / run.command from the ZIP when the
        # binary entry is bundled — they'd just confuse the
        # customer ("which one do I double-click?") and shipping
        # both makes the ZIP layout look unfinished. The README
        # branch below mirrors the decision: binary-only bundles
        # describe ONLY the .app/.exe path with no Python
        # fallback footnote.
        if not has_prebuilt_app:
            launcher = LAUNCHER_NAMES[os_name]
            body = _launcher_body(os_name)
            info = zipfile.ZipInfo(f"{prefix}/{launcher}")
            # 0o100755 = regular file with -rwxr-xr-x. zip stores Unix
            # mode in the high 16 bits of external_attr; Finder + Linux
            # honour this, so the customer can double-click without
            # `chmod +x` first. (Windows ignores the bit harmlessly.)
            info.external_attr = (0o100755 << 16) if os_name != "windows" else (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, body)
        else:
            print(f"   {LAUNCHER_NAMES[os_name]:8s}: (omitted — native binary handles launch)")

        # README copy reflects what the ZIP actually contains. A
        # binary-only bundle gets the "double-click app/NP-Create.app"
        # lead with no run.bat fallback footnote (the launcher isn't
        # in the ZIP anyway). A Python-only bundle gets the legacy
        # run.bat / run.command copy bit-for-bit.
        zf.writestr(
            f"{prefix}/README_TH.md",
            _readme(target, os_name, has_prebuilt_app=has_prebuilt_app),
        )

        # ── Windows-only Thai install fallback (INSTALL_TH.txt) ─
        # The .bat itself is ASCII-only (cmd.exe parses with the
        # local OEM codepage, so Thai bytes blow up parsing). When
        # Python is missing the .bat shows English instructions
        # only — INSTALL_TH.txt sits next to run.bat as the Thai
        # mirror so non-English customers still know what to do.
        # Skip it for binary-only ZIPs because there's no run.bat
        # to clarify the Thai instructions for in the first place.
        if os_name == "windows" and not has_prebuilt_app:
            zf.writestr(f"{prefix}/INSTALL_TH.txt", _windows_install_thai())

        # ── full Thai manual (always shipped, customer-friendly) ─
        # The README is a quick-start. The MANUAL is the real deal:
        # ~10 sections covering install / first device / streaming /
        # WiFi / troubleshooting / FAQ. We ship it as a separate
        # file so customers can keep it open in a browser tab while
        # using the app.
        manual_src = PROJECT / "docs" / "MANUAL_TH.md"
        if manual_src.is_file():
            zf.write(manual_src, f"{prefix}/MANUAL_TH.md")
            print("   manual  : MANUAL_TH.md (full guide)")
        else:
            print("   manual  : (skipped — docs/MANUAL_TH.md missing)")

    size_mb = out_zip.stat().st_size / 1024 / 1024
    print(f"   ✓ wrote {size_mb:,.1f} MB")
    return out_zip


def _ensure_prebuilt_app() -> bool:
    """Run ``tools/build_pyinstaller.py`` if the host OS's prebuilt
    artifact isn't already on disk. Returns True on success (or no-op
    when the artifact already exists), False on build failure.

    PyInstaller doesn't cross-build, so calling this from a macOS
    host can ONLY produce ``NP-Create.app`` — Windows ZIPs built on
    the same host won't gain an ``NP-Create.exe`` from this call.
    The build_one() probe correctly downgrades those ZIPs to the
    Python-only README, so the only loss is that customer Windows
    ZIPs aren't binary-ready until CI (or the admin) packs them on
    a Windows host.
    """
    prebuilt_dir = PROJECT / "dist" / "pyinstaller"
    host_artifact = (
        prebuilt_dir / "NP-Create.app" if sys.platform == "darwin"
        else prebuilt_dir / "NP-Create.exe"
    )
    if host_artifact.exists():
        print(f"[i] prebuilt already on disk: {host_artifact}")
        return True

    print(f"[i] running tools/build_pyinstaller.py "
          f"(produces {host_artifact.name} for this host)...")
    builder = HERE / "build_pyinstaller.py"
    if not builder.is_file():
        print(f"[!] {builder} missing — can't bootstrap prebuilt", file=sys.stderr)
        return False
    try:
        import subprocess
        subprocess.run(
            [sys.executable, str(builder)],
            cwd=str(PROJECT), check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[!] build_pyinstaller failed (exit {exc.returncode})",
              file=sys.stderr)
        return False
    return host_artifact.exists()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--target", choices=("customer", "admin"), default="customer",
    )
    p.add_argument(
        "--os", dest="os_name",
        choices=("windows", "macos", "linux"),
        default="windows",
    )
    p.add_argument("--all", action="store_true",
                   help="build all four combinations")
    p.add_argument(
        "--with-app", action="store_true",
        help="run tools/build_pyinstaller.py first so the resulting "
             "ZIP bundles a double-clickable NP-Create.app/.exe — "
             "customer doesn't need to install Python at all "
             "(PyInstaller cannot cross-build, so this only adds the "
             "binary for the current host OS).",
    )
    p.add_argument(
        "--dist", default=str(WORKSPACE / "dist"),
        help="output directory (default: <workspace>/dist)",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    dist = Path(args.dist).resolve()

    if args.with_app:
        if not _ensure_prebuilt_app():
            print(
                "[!] --with-app requested but prebuilt couldn't be built. "
                "Falling back to Python-only ZIPs; see error above.",
                file=sys.stderr,
            )

    if args.all:
        outs = []
        for tgt in ("customer", "admin"):
            for osn in ("windows", "macos"):
                outs.append(build_one(tgt, osn, dist))
        print("\nBuilt:")
        for p2 in outs:
            print(f"  • {p2}")
        return 0

    out = build_one(args.target, args.os_name, dist)
    print(f"\nDone: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
