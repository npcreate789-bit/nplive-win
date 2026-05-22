"""PyInstaller entry stub for NP Create.

PyInstaller resolves the entry script at build time and embeds its
``__main__`` block into the bootloader. We need a tiny shim so that
when the customer double-clicks ``NP-Create.exe`` / ``NP-Create.app``:

1. ``--studio`` is forced on (we never want the CLI behind a GUI
   binary — there's no terminal to talk to).
2. ``sys.path`` includes the bundled ``src/`` package, regardless
   of where PyInstaller chose to lay the resources out.
3. Any uncaught exception during startup ends up in a Tkinter
   dialog instead of vanishing into a closed console (very common
   PyInstaller usability gotcha — without this the customer sees
   "the icon flashed and nothing happened").
4. **v1.8.17**: a persistent ``src_overlay`` dir is inserted ahead
   of ``_MEIPASS`` on ``sys.path`` so auto-update patches (which
   write into the overlay) actually take effect on the next launch.
   Without this every patch landed in the PyInstaller scratch dir
   and silently rolled back on shutdown.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence


# Overlay layout has to match ``src.auto_update.overlay_root`` /
# ``overlay_src_dir`` exactly. We can't import that module yet —
# ``sys.path`` is still raw at this point — so we mirror the
# constants and the path-building logic here. If you change one,
# change the other. ``tests/test_auto_update_overlay.py`` pins both
# to the same value so a drift turns red.
_OVERLAY_APP_NAME = "NPCreate"
_OVERLAY_DIRNAME = "src_overlay"


def _overlay_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / _OVERLAY_APP_NAME / _OVERLAY_DIRNAME
        return Path.home() / "AppData" / "Local" / _OVERLAY_APP_NAME / _OVERLAY_DIRNAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _OVERLAY_APP_NAME / _OVERLAY_DIRNAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / _OVERLAY_APP_NAME / _OVERLAY_DIRNAME


def _overlay_src_dir() -> Path:
    return _overlay_root() / "src"


_VERSION_RE = re.compile(
    r"""version\s*:\s*str\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def _read_branding_version(branding_py: Path) -> Optional[str]:
    """Pull ``BRAND.version`` out of a ``branding.py`` file without
    importing it. We can't ``import src.branding`` to compare versions
    because picking which copy to import is the question we're trying
    to answer. Regex on the raw file is good enough — the field is a
    plain ``str = "X.Y.Z"`` literal.
    """
    try:
        text = branding_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _ver_tuple(v: str) -> tuple[int, ...]:
    head = v.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for chunk in head.split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            return ()
    return tuple(out)


def _is_overlay_newer_or_equal(overlay_ver: str, meipass_ver: str) -> bool:
    """``True`` iff the overlay's version is >= the installer's.
    Returns ``False`` on any parse failure so a malformed overlay
    can't outrank a freshly-installed .exe.
    """
    a = _ver_tuple(overlay_ver)
    b = _ver_tuple(meipass_ver)
    if not a or not b:
        return False
    return a >= b


def _seed_overlay_from(meipass_src: Path, overlay_src: Path) -> bool:
    """Copy ``meipass_src/`` → ``overlay_src/`` so the first launch
    has a tree to patch into. Returns ``True`` on success, ``False``
    if the copy failed (e.g. AV blocked the writes) — in which case
    the caller falls back to running purely from ``_MEIPASS`` and
    auto-update will refuse to apply on this machine until the
    overlay dir is writable.
    """
    try:
        overlay_src.parent.mkdir(parents=True, exist_ok=True)
        if overlay_src.exists():
            shutil.rmtree(overlay_src, ignore_errors=True)
        shutil.copytree(meipass_src, overlay_src)
        return True
    except OSError:
        # Don't block app launch on overlay setup — log to stderr so
        # support can see why patches aren't sticking on this box.
        print(
            f"[NP Create] WARNING: could not seed overlay at {overlay_src} — "
            "auto-update will not work until this dir is writable",
            file=sys.stderr,
        )
        return False


def _prepare_overlay_path() -> Optional[Path]:
    """If frozen + overlay is usable, return the dir to prepend to
    ``sys.path`` so imports of ``src.*`` resolve to the overlay
    first. Returns ``None`` when we should fall through to plain
    ``_MEIPASS`` (source mode, or overlay seed failed).

    Version-compatibility rule
    --------------------------
    A fresh installer must always win over a stale overlay — otherwise
    a customer who reinstalls a known-good .exe to recover from a bad
    patch would still see the broken patched code. So before insert,
    we compare the overlay's ``BRAND.version`` to the one PyInstaller
    just unpacked into ``_MEIPASS``; if the installer is newer (or
    the overlay's branding is unreadable), we wipe and reseed.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    meipass_src = Path(meipass) / "src"
    if not meipass_src.is_dir():
        return None
    overlay_src = _overlay_src_dir()

    if not overlay_src.is_dir():
        # First launch on this machine — seed from installer.
        if not _seed_overlay_from(meipass_src, overlay_src):
            return None
        return overlay_src.parent

    # Overlay exists. Check whether the installer is newer; if so the
    # fresh .exe wins (customer just reinstalled to fix a bad patch).
    overlay_ver = _read_branding_version(overlay_src / "branding.py")
    meipass_ver = _read_branding_version(meipass_src / "branding.py")
    if overlay_ver is None or meipass_ver is None:
        # Defensive: anything unreadable → trust the installer and
        # reseed so we don't silently keep running a broken overlay.
        if not _seed_overlay_from(meipass_src, overlay_src):
            return None
        return overlay_src.parent

    if not _is_overlay_newer_or_equal(overlay_ver, meipass_ver):
        # Installer beats overlay — reseed.
        if not _seed_overlay_from(meipass_src, overlay_src):
            return None
        return overlay_src.parent

    # Overlay is up-to-date (or ahead) — use it as-is.
    return overlay_src.parent


class _OverlaySourceFinder(importlib.abc.MetaPathFinder):
    """Override ``src.*`` lookups so patched files in the overlay
    win over PyInstaller's PYZ-frozen modules.

    Why a MetaPathFinder, not just ``sys.path``
    -------------------------------------------
    PyInstaller installs a ``FrozenImporter`` in ``sys.meta_path[0]``.
    Every ``import src.X`` hits the FrozenImporter first; only
    misses fall through to ``PathFinder`` (= ``sys.path`` lookup).
    Since PyInstaller packs every ``src.*`` module into the PYZ at
    build time, the FrozenImporter ALWAYS hits — adding the overlay
    to ``sys.path`` did literally nothing.

    The fix is a custom finder registered AHEAD of the FrozenImporter.
    For each ``src.*`` request it looks up the corresponding
    ``.py`` / ``__init__.py`` inside the overlay dir. A hit returns
    a regular ``SourceFileLoader`` spec; a miss returns ``None`` so
    Python continues down ``sys.meta_path`` and the FrozenImporter
    still serves modules the overlay doesn't carry.

    This means a partial patch (zip with only ``branding.py``) just
    overrides ``src.branding`` and lets every other ``src.*`` come
    from the PYZ. No double-load, no version skew within a single
    module, and the overlay can be safely empty (initial state on
    a brand-new install).
    """

    _PKG = "src"

    def __init__(self, overlay_src: Path) -> None:
        self.overlay_src = overlay_src

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]] = None,
        target: object = None,
    ):
        # Only intercept ``src`` and its subpackages.
        if fullname != self._PKG and not fullname.startswith(self._PKG + "."):
            return None

        if fullname == self._PKG:
            init = self.overlay_src / "__init__.py"
            if not init.is_file():
                return None
            loader = importlib.machinery.SourceFileLoader(fullname, str(init))
            spec = importlib.util.spec_from_file_location(
                fullname,
                str(init),
                loader=loader,
                submodule_search_locations=[str(self.overlay_src)],
            )
            return spec

        # Strip the "src." prefix, walk the overlay tree.
        rel = fullname[len(self._PKG) + 1:]
        parts = rel.split(".")
        pkg_init = self.overlay_src.joinpath(*parts, "__init__.py")
        if pkg_init.is_file():
            loader = importlib.machinery.SourceFileLoader(
                fullname, str(pkg_init),
            )
            return importlib.util.spec_from_file_location(
                fullname,
                str(pkg_init),
                loader=loader,
                submodule_search_locations=[str(pkg_init.parent)],
            )
        py_file = self.overlay_src.joinpath(*parts).with_suffix(".py")
        if py_file.is_file():
            loader = importlib.machinery.SourceFileLoader(
                fullname, str(py_file),
            )
            return importlib.util.spec_from_file_location(
                fullname, str(py_file), loader=loader,
            )
        # Miss — fall through to the FrozenImporter so unpatched
        # modules keep working.
        return None


def _install_overlay_finder(overlay_src: Path) -> None:
    """Insert the OverlaySourceFinder at the head of ``sys.meta_path``
    so it gets first crack at every ``src.*`` import. Idempotent: a
    second call replaces the existing finder rather than stacking
    duplicates.
    """
    sys.meta_path = [
        f for f in sys.meta_path if not isinstance(f, _OverlaySourceFinder)
    ]
    sys.meta_path.insert(0, _OverlaySourceFinder(overlay_src))


def _ensure_src_on_path() -> None:
    # When frozen, PyInstaller sets ``sys._MEIPASS`` to the temp
    # extraction dir. The ``src`` package was added via
    # ``--add-data <repo>/src:src`` so it lands at MEIPASS/src.
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        # Source mode (running from `python tools/_pyinstaller_entry.py`
        # for testing). Fall back to the project root one level up.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base not in sys.path:
        sys.path.insert(0, base)

    # v1.8.17: prefer the persistent overlay copy of src/ over the
    # PyInstaller PYZ-frozen modules. Two-step:
    #   1. Seed / refresh the overlay dir (handled by
    #      ``_prepare_overlay_path`` — copies _MEIPASS/src on first
    #      launch or after the customer reinstalls a newer .exe).
    #   2. Install a meta-path finder ahead of FrozenImporter so
    #      ``import src.X`` finds the overlay's ``.py`` first.
    overlay_parent = _prepare_overlay_path()
    if overlay_parent is not None:
        overlay_src = overlay_parent / "src"
        if overlay_src.is_dir():
            _install_overlay_finder(overlay_src)


def _show_fatal(text: str) -> None:
    """Last-ditch error dialog when even the GUI failed to start."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("NP Create — เกิดข้อผิดพลาด", text)
        root.destroy()
    except Exception:
        # No GUI possible. Print to stderr — at least cmd.exe in
        # debug mode will show it.
        print(text, file=sys.stderr)


def main() -> int:
    _ensure_src_on_path()
    try:
        from src.main import main as real_main  # noqa: WPS433
    except Exception:
        _show_fatal(
            "ไม่สามารถโหลดโปรแกรมได้ครับ\n\n"
            f"{traceback.format_exc()}\n\n"
            "กรุณาส่งข้อความนี้ให้แอดมินทาง Line @npcreate"
        )
        return 1

    # Inject --studio if the user (or Finder/Explorer) didn't pass
    # any args. PyInstaller forwards launch args verbatim.
    if not any(a.startswith("--") for a in sys.argv[1:]):
        sys.argv.append("--studio")

    try:
        return real_main() or 0
    except SystemExit:
        raise
    except Exception:
        _show_fatal(
            "โปรแกรมขัดข้องระหว่างเปิด:\n\n"
            f"{traceback.format_exc()}\n\n"
            "ส่งข้อความนี้ให้แอดมินทาง Line @npcreate"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
