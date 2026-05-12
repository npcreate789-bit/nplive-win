"""Progress callbacks during the LSPatch flow (v1.8.14).

Pre-v1.8.14 the customer stared at a frozen "กำลัง patch…" button
for 2–5 minutes during Patch. ``lspatch_pipeline`` now accepts an
optional ``progress_cb`` on every long step (pull / patch / install)
that fires across [0, 1] so the UI can render a live percentage.

The contract these tests pin:

* ``progress_cb`` is ALWAYS optional — the legacy 19 tests in
  ``test_lspatch_install_rollback`` use the no-callback signature
  and must keep passing bit-for-bit. (Verified separately.)
* When supplied, the callback fires with monotonically increasing
  ``pct`` clamped to [0, 1] and never re-fires backwards.
* The terminal call hits ``pct == 1.0`` on success.
* A buggy callback (one that raises) MUST NOT abort the underlying
  pipeline — the customer would otherwise lose the patch flow over
  an avoidable UI bug.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from src import lspatch_pipeline
from src.config import StreamConfig
from src.lspatch_pipeline import (
    InstallResult,
    LSPatchPipeline,
    PatchResult,
    PullResult,
    _fire_progress,
)


# ── helpers ────────────────────────────────────────────────────────


def _make_pipeline(tmp_path: Path) -> LSPatchPipeline:
    cfg = StreamConfig.load()
    cfg.adb_path = "adb"
    p = LSPatchPipeline(cfg)
    # Redirect caches into the tmp dir so two parallel test runs
    # don't trip over each other's pulled/ and patched/ folders.
    p.cache_dir = tmp_path / "cache"
    p.pulled_dir = p.cache_dir / "pulled"
    p.patched_dir = p.cache_dir / "patched"
    p.cache_dir.mkdir(parents=True, exist_ok=True)
    return p


class _ProgressRecorder:
    """Capture every ``(pct, msg)`` call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, str]] = []

    def __call__(self, pct: float, msg: str) -> None:
        self.calls.append((pct, msg))

    @property
    def pcts(self) -> list[float]:
        return [c[0] for c in self.calls]


# ── _fire_progress safety ──────────────────────────────────────────


class TestFireProgress:
    def test_none_callback_is_noop(self):
        # Must not raise — pipeline functions check for None before
        # calling, but the helper also guards defensively so future
        # callers don't have to.
        _fire_progress(None, 0.5, "irrelevant")

    def test_clamps_to_unit_range(self):
        seen: list[tuple[float, str]] = []
        _fire_progress(lambda p, m: seen.append((p, m)), 1.5, "over")
        _fire_progress(lambda p, m: seen.append((p, m)), -0.2, "under")
        assert seen[0] == (1.0, "over")
        assert seen[1] == (0.0, "under")

    def test_swallows_callback_exceptions(self):
        """A UI handler that crashes (e.g. a destroyed Tk widget)
        must not propagate into the pipeline. Otherwise one bad
        ``after()`` race could nuke the whole Patch flow mid-run."""
        def boom(_p, _m):
            raise RuntimeError("widget destroyed")
        # No assertion needed — the call must simply return normally.
        _fire_progress(boom, 0.5, "test")


# ── pull_tiktok progress ────────────────────────────────────────────


class TestPullProgress:
    def test_pull_fires_progress_monotonically(self, tmp_path, monkeypatch):
        """All `progress_cb` calls during pull must be non-decreasing
        in pct. A regression where the per-APK loop resets the band
        would manifest as the bar visually jumping backward."""
        p = _make_pipeline(tmp_path)

        # Stub adb shell + subprocess.run so we don't need a real
        # device. detect_tiktok returns a fixed package; pm path
        # returns 3 fake APK paths; adb pull always succeeds.
        monkeypatch.setattr(
            p, "detect_tiktok", lambda serial=None: "com.example.tt",
        )

        # Track call order: pm path → versionName → pull base.apk →
        # pull split_arm64.apk → pull split_locale.apk.
        shell_responses = {
            "pm path": (
                "package:/data/app/com.example.tt/base.apk\n"
                "package:/data/app/com.example.tt/split_arm64.apk\n"
                "package:/data/app/com.example.tt/split_locale.apk\n"
            ),
            "versionName": "  versionName=1.2.3\n",
        }

        def _fake_shell(cmd: str, serial=None) -> str:
            if "pm path" in cmd:
                return shell_responses["pm path"]
            if "versionName" in cmd:
                return shell_responses["versionName"]
            return ""
        monkeypatch.setattr(p, "_adb_shell", _fake_shell)

        # `subprocess.run` is called only for `adb pull`; stub OK.
        def _fake_run(cmd, capture_output, text, timeout, check):
            # Write a stub file at the dst path so unwrap_lspatched
            # has something to read (it tries to open as zip — a
            # non-zip file returns empty list which is fine).
            dst = Path(cmd[-1])
            dst.write_bytes(b"PK\x03\x04 stub apk")
            return mock.Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(
            lspatch_pipeline.subprocess, "run", _fake_run,
        )

        # Disable the unwrap to avoid touching zipfile internals on
        # a non-zip file.
        monkeypatch.setattr(p, "_unwrap_lspatched", lambda xs: xs)

        rec = _ProgressRecorder()
        result = p.pull_tiktok(progress_cb=rec)

        assert result.ok, f"pull should succeed in stub mode: {result.error}"
        assert rec.calls, "progress_cb must fire at least once"
        # Monotonic non-decreasing.
        for i in range(1, len(rec.pcts)):
            assert rec.pcts[i] >= rec.pcts[i - 1] - 1e-9, (
                f"progress went backwards at call {i}: "
                f"{rec.pcts[i - 1]:.3f} → {rec.pcts[i]:.3f}"
            )
        # Lands at 1.0 on success.
        assert rec.pcts[-1] == 1.0
        # Reasonable coverage: at least one mid-flight update per APK.
        assert len(rec.calls) >= 5, (
            "expected at least probe + 3 per-APK + finish callbacks, "
            f"got {len(rec.calls)}"
        )

    def test_pull_no_callback_keeps_legacy_behaviour(self, tmp_path, monkeypatch):
        """Default ``progress_cb=None`` must run the exact code path
        the v1.8.13-and-earlier callers depended on. We can't byte-
        compare easily, so we assert the same result shape on the
        same stub inputs."""
        p = _make_pipeline(tmp_path)
        monkeypatch.setattr(
            p, "detect_tiktok", lambda serial=None: "com.example.tt",
        )

        def _shell(cmd: str, serial=None) -> str:
            if "pm path" in cmd:
                return "package:/data/app/foo/base.apk\n"
            if "versionName" in cmd:
                return "  versionName=9.9.9\n"
            return ""
        monkeypatch.setattr(p, "_adb_shell", _shell)

        def _run(cmd, capture_output, text, timeout, check):
            Path(cmd[-1]).write_bytes(b"x")
            return mock.Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(lspatch_pipeline.subprocess, "run", _run)
        monkeypatch.setattr(p, "_unwrap_lspatched", lambda xs: xs)

        result = p.pull_tiktok()  # no progress_cb
        assert result.ok
        assert result.version_name == "9.9.9"
        assert len(result.apks) == 1

    def test_pull_callback_exception_doesnt_break(self, tmp_path, monkeypatch):
        """A buggy UI handler must not propagate into the pipeline
        — otherwise one destroyed Tk widget kills the Patch flow."""
        p = _make_pipeline(tmp_path)
        monkeypatch.setattr(
            p, "detect_tiktok", lambda serial=None: "com.example.tt",
        )
        monkeypatch.setattr(
            p, "_adb_shell",
            lambda cmd, serial=None: (
                "package:/data/app/foo/base.apk\n"
                if "pm path" in cmd
                else "  versionName=1.0\n" if "versionName" in cmd
                else ""
            ),
        )

        def _run(cmd, capture_output, text, timeout, check):
            Path(cmd[-1]).write_bytes(b"x")
            return mock.Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(lspatch_pipeline.subprocess, "run", _run)
        monkeypatch.setattr(p, "_unwrap_lspatched", lambda xs: xs)

        def boom(_p, _m):
            raise RuntimeError("Tk widget destroyed")

        result = p.pull_tiktok(progress_cb=boom)
        # Must still succeed.
        assert result.ok, f"buggy callback broke the pipeline: {result.error}"


# ── patch progress ─────────────────────────────────────────────────


class TestPatchProgress:
    def test_patch_no_callback_uses_legacy_path(self, tmp_path, monkeypatch):
        """The default-None branch must use the simple
        ``subprocess.run`` path (no Popen + ticker) so the 19
        existing tests stay valid. We assert by checking that
        ``subprocess.Popen`` was NEVER called."""
        p = _make_pipeline(tmp_path)

        # Stub probe_tools to a successful ToolStatus.
        from src.lspatch_pipeline import ToolStatus
        st = ToolStatus(
            java=tmp_path / "java",
            lspatch=tmp_path / "lspatch.jar",
            vcam_apk=tmp_path / "vcam.apk",
            adb="adb",
            ok=True,
        )
        monkeypatch.setattr(p, "probe_tools", lambda: st)

        # Pre-create a fake output file so the post-run glob finds it.
        def _fake_run(cmd, capture_output, text, timeout, check, env=None):
            (p.patched_dir).mkdir(parents=True, exist_ok=True)
            (p.patched_dir / "base-lspatched.apk").write_bytes(b"PK")
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        monkeypatch.setattr(lspatch_pipeline.subprocess, "run", _fake_run)

        popen_calls: list = []
        monkeypatch.setattr(
            lspatch_pipeline.subprocess, "Popen",
            lambda *a, **kw: popen_calls.append((a, kw))
            or pytest.fail("Popen called in no-callback path"),
        )

        result = p.patch([tmp_path / "in.apk"])
        assert result.ok
        assert not popen_calls, (
            "no-callback path must use subprocess.run (legacy), not Popen"
        )

    def test_patch_with_callback_uses_popen_and_fires_progress(
        self, tmp_path, monkeypatch,
    ):
        """With progress_cb supplied, the patch must:
        1. Spawn LSPatch via Popen (so the ticker thread can fire
           interpolated updates while we wait).
        2. Emit at least the initial probe/setup callbacks AND a
           terminal 1.0 callback on success.
        """
        p = _make_pipeline(tmp_path)

        from src.lspatch_pipeline import ToolStatus
        st = ToolStatus(
            java=tmp_path / "java",
            lspatch=tmp_path / "lspatch.jar",
            vcam_apk=tmp_path / "vcam.apk",
            adb="adb",
            ok=True,
        )
        monkeypatch.setattr(p, "probe_tools", lambda: st)

        # Fake Popen that returns immediately with rc=0 and a pre-
        # created output APK so we don't need a real Java install.
        class _FakeProc:
            def __init__(self):
                self.returncode = 0
                self.stdout = iter([])
                self.stderr = iter([])
                # Drop the expected output file so the post-run
                # glob finds it.
                p.patched_dir.mkdir(parents=True, exist_ok=True)
                (p.patched_dir / "base-lspatched.apk").write_bytes(b"PK")

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        popen_calls: list = []

        def _fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return _FakeProc()
        monkeypatch.setattr(
            lspatch_pipeline.subprocess, "Popen", _fake_popen,
        )

        rec = _ProgressRecorder()
        result = p.patch([tmp_path / "in.apk"], progress_cb=rec)

        assert result.ok, f"patch should succeed in stub: {result.error}"
        assert popen_calls, "callback path must use Popen for streaming"
        assert rec.calls, "progress_cb must fire at least once"
        # Terminal callback hits 1.0.
        assert rec.pcts[-1] == 1.0
        # And we got the initial setup callbacks.
        assert any(p_ <= 0.10 for p_ in rec.pcts), (
            "expected at least one early-stage callback (probe + prep)"
        )


# ── install progress ───────────────────────────────────────────────


class TestInstallProgress:
    def test_install_fires_terminal_progress_on_success(
        self, tmp_path, monkeypatch,
    ):
        p = _make_pipeline(tmp_path)
        monkeypatch.setattr(
            p, "_adb_shell",
            lambda cmd, serial=None: "  Signing certificate: ABCD\n",
        )

        def _fake_run(cmd, capture_output, text, timeout, check):
            # adb uninstall always succeeds; install-multiple emits
            # 'Success' on stdout (what the parser looks for).
            if "uninstall" in cmd:
                return mock.Mock(returncode=0, stdout="Success", stderr="")
            if "install-multiple" in cmd:
                return mock.Mock(returncode=0, stdout="Success", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(lspatch_pipeline.subprocess, "run", _fake_run)

        rec = _ProgressRecorder()
        apks = [tmp_path / "patched-base.apk"]
        apks[0].write_bytes(b"PK")
        result = p.install(
            package="com.example.tt", patched_apks=apks,
            progress_cb=rec,
        )
        assert result.ok
        assert rec.pcts[-1] == 1.0
        # Distinguishable mid-flight messages: uninstall + install.
        labels = " ".join(c[1] for c in rec.calls)
        assert "ลบ TikTok เดิม" in labels
        assert "ติดตั้ง patched" in labels

    def test_install_no_callback_keeps_legacy_signature(
        self, tmp_path, monkeypatch,
    ):
        """Existing callers (e.g. WizardPage._run_wizard_patch as of
        v1.8.13) call install() without progress_cb. Default-None
        must keep the v1.8.13 behaviour bit-for-bit."""
        p = _make_pipeline(tmp_path)
        monkeypatch.setattr(p, "_adb_shell", lambda cmd, serial=None: "")
        monkeypatch.setattr(
            lspatch_pipeline.subprocess, "run",
            lambda *a, **kw: mock.Mock(
                returncode=0, stdout="Success", stderr="",
            ),
        )

        apks = [tmp_path / "x.apk"]
        apks[0].write_bytes(b"PK")
        result = p.install(package="com.example.tt", patched_apks=apks)
        assert result.ok
