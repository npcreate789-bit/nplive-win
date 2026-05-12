"""Persistent update preferences — JSON load / save contract.

We exercise the v1.8.13 prefs module that backs the Settings →
"อัปเดต" card. The headline guarantee is *resilience*: every
public function returns a valid ``UpdatePrefs`` instance even
when the on-disk file is missing, corrupt, schema-drifted, or
permissioned-out, because a broken prefs file must NEVER stop the
app from launching.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.update_prefs import UpdatePrefs


class TestDefaults:
    def test_no_file_returns_defaults(self, tmp_path):
        p = UpdatePrefs.load(project_root=tmp_path)
        assert p.install_on_close is False
        assert p.auto_prefetch is True
        assert p.last_check_ts == 0.0

    def test_defaults_install_on_close_is_off(self, tmp_path):
        """Opt-in only: applying a patch on close changes shutdown
        timing (extra ~2 s while apply_patch runs). Defaulting to
        True would surprise customers who close-out fast."""
        assert UpdatePrefs.load(project_root=tmp_path).install_on_close is False

    def test_defaults_auto_prefetch_is_on(self, tmp_path):
        """The banner appearing already means the customer's
        attention is on the update — pre-downloading a few hundred
        KB in the background is essentially free, and makes the
        eventual click instant. Worth defaulting on."""
        assert UpdatePrefs.load(project_root=tmp_path).auto_prefetch is True


class TestSaveLoad:
    def test_save_round_trips(self, tmp_path):
        p = UpdatePrefs(install_on_close=True, auto_prefetch=False)
        p.last_check_ts = 1234567890.0
        ok = p.save(project_root=tmp_path)
        assert ok is True

        loaded = UpdatePrefs.load(project_root=tmp_path)
        assert loaded.install_on_close is True
        assert loaded.auto_prefetch is False
        assert loaded.last_check_ts == 1234567890.0

    def test_save_creates_cache_dir(self, tmp_path):
        """First launch with no cache/ dir must still succeed."""
        # tmp_path/cache doesn't exist yet.
        UpdatePrefs(install_on_close=True).save(project_root=tmp_path)
        assert (tmp_path / "cache" / "update_prefs.json").is_file()

    def test_save_is_atomic(self, tmp_path):
        """Crash mid-write must not leave a half-formatted file.
        We can't simulate a crash here, but we can verify the
        intermediate ``.tmp`` file isn't lingering after save."""
        UpdatePrefs(install_on_close=True).save(project_root=tmp_path)
        cache = tmp_path / "cache"
        assert (cache / "update_prefs.json").is_file()
        # Any .tmp sidecar must be renamed away on success.
        stale = list(cache.glob("*.tmp"))
        assert stale == [], f"stale tmp files: {stale}"


class TestResilience:
    def test_corrupt_json_returns_defaults(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "update_prefs.json").write_text(
            "{not even json", encoding="utf-8",
        )
        p = UpdatePrefs.load(project_root=tmp_path)
        # All defaults — corrupt file must not block launch.
        assert p == UpdatePrefs()

    def test_non_object_json_returns_defaults(self, tmp_path):
        """JSON that's syntactically valid but not a dict (e.g. a
        list, or just `null`) must degrade gracefully too."""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "update_prefs.json").write_text("[1,2,3]", encoding="utf-8")
        assert UpdatePrefs.load(project_root=tmp_path) == UpdatePrefs()

    def test_forward_compat_drops_unknown_keys(self, tmp_path):
        """A future build that ships extra prefs keys must still be
        loadable by this older code path — dropping unknown keys is
        the agreed compatibility direction (you can upgrade, not
        downgrade)."""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "update_prefs.json").write_text(
            json.dumps({
                "install_on_close": True,
                "auto_prefetch": False,
                "future_field_we_dont_know_about": "abc",
            }),
            encoding="utf-8",
        )
        p = UpdatePrefs.load(project_root=tmp_path)
        assert p.install_on_close is True
        assert p.auto_prefetch is False

    def test_partial_keys_use_defaults_for_missing(self, tmp_path):
        """Old on-disk file that pre-dates one of our fields must
        gain the new field with the dataclass default."""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "update_prefs.json").write_text(
            json.dumps({"install_on_close": True}),  # only one key
            encoding="utf-8",
        )
        p = UpdatePrefs.load(project_root=tmp_path)
        assert p.install_on_close is True
        # Other fields fall back to defaults.
        assert p.auto_prefetch is True
        assert p.last_check_ts == 0.0


class TestMarkChecked:
    def test_stamps_current_time(self, tmp_path):
        p = UpdatePrefs.load(project_root=tmp_path)
        before = time.time()
        p.mark_checked(project_root=tmp_path)
        after = time.time()
        assert before <= p.last_check_ts <= after

    def test_persists_across_load(self, tmp_path):
        p = UpdatePrefs.load(project_root=tmp_path)
        p.mark_checked(project_root=tmp_path, now=1700000000.0)
        loaded = UpdatePrefs.load(project_root=tmp_path)
        assert loaded.last_check_ts == 1700000000.0
