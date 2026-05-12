"""README selection for build_release: native binary vs Python-only.

v1.8.13 added the ``has_prebuilt_app`` switch so the customer ZIP's
README reflects what's actually inside the archive:

* ZIP carries ``app/NP-Create.app`` / ``app/NP-Create.exe`` → lead
  with "double-click the binary, no Python install needed".
* ZIP is the legacy Python-only layout → keep the v1.8.12 wording
  bit-for-bit (run.bat / run.command primary, Python 3.13 required).

These tests pin both branches so a future "simplify the README"
refactor can't accidentally regress the customer instructions for
either bundle shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# build_release lives under tools/ which isn't a package — make it
# importable just for the test.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import build_release  # noqa: E402


class TestReadmeWithoutPrebuilt:
    """Legacy path — bundle ships run.bat / run.command only."""

    def test_windows_keeps_run_bat_lead(self):
        text = build_release._readme(
            "customer", "windows", has_prebuilt_app=False,
        )
        assert "run.bat" in text
        # Must NOT mention the binary path — that would mislead a
        # customer to look for a file that isn't in the ZIP.
        assert "NP-Create.exe" not in text
        # Python install instruction still leads.
        assert "Python 3.13" in text

    def test_macos_keeps_run_command_lead(self):
        text = build_release._readme(
            "customer", "macos", has_prebuilt_app=False,
        )
        assert "run.command" in text
        assert "NP-Create.app" not in text
        assert "Python 3.13" in text

    def test_default_is_python_only(self):
        """Calling _readme without the kwarg = legacy behaviour.
        That's the contract that lets pre-v1.8.13 callers (admin
        scripts, CI workflows) keep working unchanged."""
        text = build_release._readme("customer", "macos")
        # Should match the explicit False branch.
        explicit = build_release._readme(
            "customer", "macos", has_prebuilt_app=False,
        )
        assert text == explicit


class TestReadmeWithPrebuilt:
    """v1.8.13 ship: bundle carries the PyInstaller .app/.exe.

    Design choice — when the binary is shipped, the README must NOT
    mention run.bat / run.command. That's because build_release.py
    OMITS the launcher script from the bundle entirely (one entry
    point, no ambiguity); telling the customer to look for a file
    that isn't there would be a regression bug, not a feature.
    """

    def test_windows_leads_with_exe(self):
        text = build_release._readme(
            "customer", "windows", has_prebuilt_app=True,
        )
        # Primary entry — the easy-mode binary path. The path is
        # the ZIP root (no ``app/`` prefix) so the icon is visible
        # immediately when the customer unzips.
        assert "`NP-Create.exe`" in text
        # Defensive: the OLD ``app/NP-Create.exe`` path must NOT
        # appear — that's the v1.8.12 layout we shipped briefly and
        # which the v1.8.13 layout supersedes. Catching a regression
        # here means we can't accidentally re-introduce the
        # subdirectory and confuse customers reading two different
        # READMEs.
        assert "app/NP-Create.exe" not in text
        # The lead-in copy explicitly says "no Python install needed".
        assert "ไม่ต้องลง Python" in text
        # run.bat MUST NOT appear in the customer README when the
        # binary is shipped — it isn't in the ZIP, so a mention
        # would send the customer hunting for a missing file.
        assert "run.bat" not in text
        # Same for the demoted "ทางเลือก: ใช้ Python" sub-heading
        # — it's the marker for the dual-launcher layout we used
        # before v1.8.13 and is no longer present.
        assert "ทางเลือก" not in text

    def test_macos_leads_with_app(self):
        text = build_release._readme(
            "customer", "macos", has_prebuilt_app=True,
        )
        # ZIP-root placement — no ``app/`` prefix.
        assert "`NP-Create.app`" in text
        assert "app/NP-Create.app" not in text
        assert "ไม่ต้องลง Python" in text
        # No run.command reference — the launcher script was
        # removed from binary-bundled ZIPs in v1.8.13.
        assert "run.command" not in text
        assert "ทางเลือก" not in text

    def test_admin_bundle_documents_with_app_flag(self):
        """Admin README must surface the new --with-app build option
        so the next admin who clones the repo discovers the
        zero-Python-install ZIP shape without grepping the source.
        """
        text = build_release._readme(
            "admin", "windows", has_prebuilt_app=True,
        )
        assert "--with-app" in text


class TestReadmeStability:
    """Bit-for-bit stability for the legacy README — the Python-only
    ZIP that pre-dates v1.8.13 must read EXACTLY the same after this
    refactor. Otherwise an admin who diffed two builds would see a
    spurious copy change and worry the launcher itself changed.
    """

    @pytest.mark.parametrize("os_name", ["windows", "macos"])
    def test_legacy_copy_unchanged_for_customer(self, os_name):
        text = build_release._readme(
            "customer", os_name, has_prebuilt_app=False,
        )
        # Sentinels from the pre-v1.8.13 README that must persist.
        assert "## เริ่มต้นใช้งานเร็ว ๆ (Quick Start)" in text
        # NO new "(Quick Start) ⚡" decoration — that's the v1.8.13
        # marker which only the prebuilt branch should carry.
        assert "Quick Start) ⚡" not in text
        # No "ทางเลือก" footnote in the legacy path either.
        assert "ทางเลือก" not in text
