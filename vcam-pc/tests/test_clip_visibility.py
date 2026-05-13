"""Clip Show/Hide toggle plumbing.

Covers the v1.8.15 ``set_clip_visibility`` API on
``HookModePipeline`` plus the ``DeviceEntry.clip_showing``
persistence that the Show/Hide button in the encode card depends on.

The toggle is broadcast-only — it flips vcam mode between 2
(replace camera with pushed MP4) and 0 (passthrough) without
re-encoding or re-pushing the file. These tests pin:

1. The exact ``adb shell am broadcast`` argv we fire for show / hide,
   because the LSPatched ``VCamModeReceiver`` keys off ``mode`` and
   ``forceReload`` extras specifically — a typo there silently breaks
   the toggle without any UI feedback.
2. The new ``clip_showing`` field round-trips through devices.json
   so the button label survives an app restart.
"""
from __future__ import annotations

import json

from src.config import StreamConfig
from src.customer_devices import DeviceEntry, DeviceLibrary
from src.hook_mode import HookModePipeline


# ── set_clip_visibility broadcast format ────────────────────────────


class _RunRecorder:
    """Capture every subprocess.run cmd argv + return a fake success
    result. Mirrors the lightweight stub used elsewhere in this
    suite (test_hook_status, test_lspatch_*) — we don't want a
    real adb spawn here."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


def _pipeline_with_fake_adb(monkeypatch, adb_path: str = "/fake/adb"):
    cfg = StreamConfig()
    cfg.adb_path = adb_path
    pipe = HookModePipeline(cfg)
    # Force _resolve_adb to skip its on-disk lookups — the recorder
    # already knows the path.
    monkeypatch.setattr(pipe, "_resolve_adb", lambda: adb_path)
    return pipe


def test_show_clip_broadcasts_mode_2_with_reload_and_touches_flag(monkeypatch):
    """``show=True`` must (a) broadcast SET_MODE mode=2 WITH
    ``forceReload=true`` so MediaPlayer restarts from frame 0, and
    (b) touch the sentinel flag so ``CameraHook.resolvedMode()``
    can't fall back to 0 if the broadcast receiver missed it.

    The initial v1.8.15 attempt sent only the broadcast and skipped
    forceReload to "avoid a visible gap on the live feed" — that
    broke realtime feedback because MediaPlayer would resume from
    its background position, often desynced from the audio that
    the receiver restarts fresh on every Show."""
    rec = _RunRecorder()
    monkeypatch.setattr("src.hook_mode.subprocess.run", rec)

    pipe = _pipeline_with_fake_adb(monkeypatch)
    pipe.set_clip_visibility(
        show=True, serial="ABC123", tiktok_pkg="com.zhiliaoapp.musically",
    )

    # Two calls: broadcast, then sentinel touch.
    assert len(rec.calls) == 2, (
        f"expected broadcast + sentinel-touch; got {len(rec.calls)} calls"
    )

    broadcast = rec.calls[0]
    assert broadcast[0] == "/fake/adb"
    assert broadcast[1:3] == ["-s", "ABC123"]
    assert "broadcast" in broadcast
    assert "com.livemobillrerun.vcam.SET_MODE" in broadcast
    assert "com.zhiliaoapp.musically" in broadcast
    mi = broadcast.index("--ei")
    assert broadcast[mi + 1] == "mode" and broadcast[mi + 2] == "2"
    assert "forceReload" in broadcast, (
        "Show must include forceReload=true so MediaPlayer rebuilds "
        "from disk — otherwise the customer sees a stale frame "
        "wherever the background-looping player happened to be"
    )

    sentinel = rec.calls[1]
    assert "touch" in sentinel
    assert "/data/local/tmp/vcam_enabled" in sentinel


def test_hide_clip_broadcasts_mode_0_and_removes_flag(monkeypatch):
    """``show=False`` must (a) broadcast SET_MODE mode=0 AND (b)
    delete the sentinel flag.

    Without the rm, ``CameraHook.resolvedMode()`` (see Android
    side at CameraHook.kt:230) returns 2 whenever the flag file
    exists, even after the broadcast sets ``currentMode=0`` — so
    Hide silently no-ops. That was the v1.8.15 "ไม่ realtime"
    bug; this test pins the fix in place."""
    rec = _RunRecorder()
    monkeypatch.setattr("src.hook_mode.subprocess.run", rec)

    pipe = _pipeline_with_fake_adb(monkeypatch)
    pipe.set_clip_visibility(
        show=False, serial="ABC123", tiktok_pkg="com.zhiliaoapp.musically",
    )

    assert len(rec.calls) == 2

    broadcast = rec.calls[0]
    mi = broadcast.index("--ei")
    assert broadcast[mi + 1] == "mode" and broadcast[mi + 2] == "0", (
        "hide must use mode=0 (passthrough), not 2"
    )
    # forceReload is omitted for Hide — there's nothing on the
    # passthrough side to reload.
    assert "forceReload" not in broadcast

    sentinel = rec.calls[1]
    assert "rm" in sentinel
    assert "/data/local/tmp/vcam_enabled" in sentinel


def test_force_reload_still_default_for_push_completion(monkeypatch):
    """The push-completion path (``_broadcast_force_reload`` with
    no kwargs) MUST still send forceReload=true — that's what
    makes the freshly-pushed clip kick in on the next frame.
    Regression guard for the kwarg refactor."""
    rec = _RunRecorder()
    monkeypatch.setattr("src.hook_mode.subprocess.run", rec)

    pipe = _pipeline_with_fake_adb(monkeypatch)
    pipe._broadcast_force_reload(
        serial="ABC123", tiktok_pkg="com.zhiliaoapp.musically",
    )

    assert len(rec.calls) == 1
    cmd = rec.calls[0]
    assert "forceReload" in cmd
    fr_idx = cmd.index("forceReload")
    assert cmd[fr_idx + 1] == "true"
    mi = cmd.index("--ei")
    assert cmd[mi + 1] == "mode" and cmd[mi + 2] == "2"


# ── DeviceEntry.clip_showing persistence ────────────────────────────


def test_clip_showing_defaults_to_true():
    """Backward-compat: existing devices (and brand-new entries) must
    default to ``clip_showing=True`` so the Show/Hide button reads
    "หยุดแสดงคลิป" first time it appears — matching the historical
    "push → auto-show" behaviour customers are used to."""
    e = DeviceEntry(serial="X")
    assert e.clip_showing is True


def test_clip_showing_round_trips_via_devices_json(tmp_path):
    path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.set_clip_showing("AB123", False)
    lib.save(path)

    reloaded = DeviceLibrary.load(path)
    e = reloaded.get("AB123")
    assert e is not None
    assert e.clip_showing is False, (
        "clip_showing=False must survive the JSON round-trip"
    )


def test_clip_showing_legacy_devices_json_defaults_true(tmp_path):
    """An older devices.json (pre-v1.8.15) has no clip_showing key
    at all. Loading it must default the field to True, not crash."""
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps({
            "entries": {
                "AB123": {
                    "label": "Test", "model": "TestPhone",
                    # …deliberately no clip_showing field…
                },
            },
        }),
        encoding="utf-8",
    )
    lib = DeviceLibrary.load(path)
    e = lib.get("AB123")
    assert e is not None
    assert e.clip_showing is True


def test_set_clip_showing_noop_on_unknown_serial():
    """set_clip_showing must not raise (or upsert) when given a
    serial we've never seen — the UI button can't be visible for
    such a device anyway, but the API should still degrade
    gracefully."""
    lib = DeviceLibrary()
    lib.set_clip_showing("NEVER-SEEN", False)
    assert lib.get("NEVER-SEEN") is None
