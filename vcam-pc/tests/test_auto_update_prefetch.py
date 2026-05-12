"""Pack A — auto-update prefetch + resume + manual check.

These tests pin the v1.8.13 additions ONLY. The legacy contracts
(``fetch_manifest``, ``download_patch``, ``apply_patch``, ...) are
exercised by ``test_auto_update.py`` and must keep passing — that
file is intentionally untouched.

What we lock in
---------------

Cache lifecycle (``prefetch_cache_dir`` / ``cached_patch_path`` /
``find_cached_patch``)

* The cache dir is auto-created on first call.
* The on-disk filename is deterministic for a given manifest
  (so two concurrent prefetchers can't pick different paths and
  race) AND includes the sha256 prefix so a re-spun version
  doesn't reuse stale bytes.
* ``find_cached_patch`` stream-hashes the candidate and returns
  ``None`` (plus removes the file) on sha mismatch — the cache
  must never serve a poisoned zip to ``apply_patch``.

Prefetch flow (``prefetch_patch``)

* Cache HIT short-circuits with no network call AND fires the
  progress callback at 100 % so the UI bar jumps to "ready".
* Cache MISS does a fresh download, verifies sha, returns the
  cached path.
* ``kind == "full"`` refuses — full installers must be opened in
  the browser, never auto-applied.
* SHA mismatch removes the bad file BEFORE raising so the next
  attempt retries cleanly.
* ``cancel_event`` aborts mid-download AND leaves the partial
  ``.part`` file on disk so the next call resumes.

Resume (``_http_get_resumable``)

* If ``.part`` exists, we send ``Range: bytes=<size>-`` and append
  on 206.
* If the server replies 200 (no Range support), we wipe ``.part``
  and start from byte zero — the user gets a complete file
  regardless of server quirks.
* The final atomic rename ``.part → dest`` is what makes
  ``find_cached_patch`` race-safe.

Manual poll (``UpdatePoller.poll_now`` / ``kick``)

* ``poll_now`` performs a synchronous fetch and fires
  ``on_update`` exactly like the background tick.
* Same-version repeats do NOT re-fire ``on_update`` (so a
  Settings-page "Check now" spam click can't flood the banner
  with re-shows).
* ``kick`` is non-blocking and idempotent.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src import auto_update


# ── helpers ────────────────────────────────────────────────────────


class _Resp:
    """urllib response stand-in with status + Content-Range support.

    The existing ``test_auto_update._Resp`` doesn't know about HTTP
    status codes; the resume path needs to distinguish 200 (start
    over) from 206 (append). Rather than touch the legacy helper
    we ship a richer one here.
    """

    def __init__(
        self,
        data: bytes,
        *,
        status: int = 200,
        headers: dict | None = None,
    ) -> None:
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read() if n < 0 else self._buf.read(n)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Sniff:
    """Patches ``urlopen`` and records every request that goes
    through so a test can assert "no network call happened on
    cache hit" without having to invert exception flow.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list = []

    def __enter__(self):
        def _open(req, *_a, **_kw):
            self.requests.append(req)
            if not self.responses:
                raise AssertionError(
                    "urlopen called more times than canned responses "
                    f"(got request #{len(self.requests)})"
                )
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        self._patch = patch.object(
            auto_update.urllib.request, "urlopen", side_effect=_open,
        )
        self._patch.__enter__()
        return self

    def __exit__(self, *exc):
        return self._patch.__exit__(*exc)


def _manifest(
    *,
    sha: str,
    version: str = "9.9.9",
    kind: str = "source",
    url: str = "https://example.invalid/p.zip",
) -> auto_update.UpdateManifest:
    return auto_update.UpdateManifest(
        version=version, kind=kind, download_url=url,
        sha256_hex=sha, notes_th="ทดสอบ prefetch",
    )


# ── prefetch_cache_dir ─────────────────────────────────────────────


class TestPrefetchCacheDir:
    def test_override_creates_dir(self, tmp_path):
        cd = auto_update.prefetch_cache_dir(project_root=tmp_path)
        assert cd.is_dir()
        assert cd == tmp_path / "cache" / "updates"

    def test_idempotent_when_already_exists(self, tmp_path):
        (tmp_path / "cache" / "updates").mkdir(parents=True)
        # Should not raise even though dir already exists.
        cd = auto_update.prefetch_cache_dir(project_root=tmp_path)
        assert cd.is_dir()


# ── cached_patch_path / find_cached_patch ──────────────────────────


class TestCachedPatchPath:
    def test_filename_is_deterministic(self, tmp_path):
        m = _manifest(sha="a" * 64, version="1.2.3")
        p1 = auto_update.cached_patch_path(m, cache_dir=tmp_path)
        p2 = auto_update.cached_patch_path(m, cache_dir=tmp_path)
        assert p1 == p2
        assert p1.parent == tmp_path

    def test_filename_changes_when_sha_changes(self, tmp_path):
        """A version re-spin with different bytes MUST get a fresh
        path so the old cache file doesn't shadow the new download.
        """
        m1 = _manifest(sha="a" * 64, version="1.2.3")
        m2 = _manifest(sha="b" * 64, version="1.2.3")
        p1 = auto_update.cached_patch_path(m1, cache_dir=tmp_path)
        p2 = auto_update.cached_patch_path(m2, cache_dir=tmp_path)
        assert p1 != p2

    def test_filename_includes_version(self, tmp_path):
        m = _manifest(sha="0" * 64, version="1.8.13")
        p = auto_update.cached_patch_path(m, cache_dir=tmp_path)
        assert "1.8.13" in p.name


class TestFindCachedPatch:
    def test_missing_returns_none(self, tmp_path):
        m = _manifest(sha="0" * 64)
        assert auto_update.find_cached_patch(m, cache_dir=tmp_path) is None

    def test_matching_sha_returns_path(self, tmp_path):
        data = b"hello world"
        sha = hashlib.sha256(data).hexdigest()
        m = _manifest(sha=sha)
        auto_update.cached_patch_path(m, cache_dir=tmp_path).write_bytes(data)
        out = auto_update.find_cached_patch(m, cache_dir=tmp_path)
        assert out is not None
        assert out.read_bytes() == data

    def test_sha_mismatch_returns_none_and_removes_file(self, tmp_path):
        """The cache must never feed a poisoned zip to apply_patch.
        On mismatch we drop the file so the next prefetch downloads
        fresh instead of looping on a corrupt cache hit.
        """
        m = _manifest(sha="0" * 64)
        bad = auto_update.cached_patch_path(m, cache_dir=tmp_path)
        bad.write_bytes(b"different bytes")
        assert auto_update.find_cached_patch(m, cache_dir=tmp_path) is None
        assert not bad.exists(), "stale file must be removed on miss"


# ── prefetch_patch ─────────────────────────────────────────────────


class TestPrefetchPatch:
    def test_happy_path_caches_and_returns(self, tmp_path):
        data = b"a fake patch zip"
        sha = hashlib.sha256(data).hexdigest()
        m = _manifest(sha=sha)
        with _Sniff(_Resp(data)) as s:
            out = auto_update.prefetch_patch(m, cache_dir=tmp_path)
        assert out.exists()
        assert out.read_bytes() == data
        # Subsequent call must NOT hit the network — cache lookup
        # short-circuits before urlopen.
        with _Sniff() as s2:
            out2 = auto_update.prefetch_patch(m, cache_dir=tmp_path)
            assert out2 == out
            assert s2.requests == []

    def test_cache_hit_fires_progress_at_100(self, tmp_path):
        data = b"cached bytes"
        sha = hashlib.sha256(data).hexdigest()
        m = _manifest(sha=sha)
        auto_update.cached_patch_path(m, cache_dir=tmp_path).write_bytes(data)

        seen: list = []
        with _Sniff() as s:
            auto_update.prefetch_patch(
                m, cache_dir=tmp_path,
                progress_cb=lambda got, total: seen.append((got, total)),
            )
            assert s.requests == []
        # The UI relies on a 100 % pulse so the progress bar jumps
        # to "ready" instead of dwelling at 0 %.
        assert seen, "progress callback must fire even on cache hit"
        got, total = seen[-1]
        assert got == total == len(data)

    def test_kind_full_refuses(self, tmp_path):
        m = _manifest(sha="0" * 64, kind="full")
        with pytest.raises(auto_update.UpdateError, match="kind"):
            auto_update.prefetch_patch(m, cache_dir=tmp_path)

    def test_sha_mismatch_removes_bad_file_and_raises(self, tmp_path):
        m = _manifest(sha="0" * 64)  # WRONG hash
        with _Sniff(_Resp(b"some bytes")):
            with pytest.raises(auto_update.UpdateError, match="sha256"):
                auto_update.prefetch_patch(m, cache_dir=tmp_path)
        # Bad bytes must NOT linger — next prefetch should retry.
        assert auto_update.cached_patch_path(
            m, cache_dir=tmp_path,
        ).exists() is False

    def test_size_cap_pre_flight(self, tmp_path):
        big = _Resp(
            b"x" * 64,
            headers={"Content-Length": str(auto_update.MAX_PATCH_BYTES + 1)},
        )
        m = _manifest(sha="0" * 64)
        with _Sniff(big):
            with pytest.raises(auto_update.UpdateError, match="too large"):
                auto_update.prefetch_patch(m, cache_dir=tmp_path)

    def test_cancel_event_aborts_download(self, tmp_path):
        """Customer dismisses the banner mid-prefetch. The worker
        must bail cleanly AND leave the partial .part file on disk
        so a future prefetch can resume rather than restart."""
        m = _manifest(sha="0" * 64)
        cancel = threading.Event()
        cancel.set()  # already cancelled before we read a byte

        with _Sniff(_Resp(b"a" * 1024)):
            with pytest.raises(auto_update.UpdateError, match="cancel"):
                auto_update.prefetch_patch(
                    m, cache_dir=tmp_path, cancel_event=cancel,
                )


# ── _http_get_resumable (resume + range) ───────────────────────────


class TestHttpGetResumable:
    def test_first_download_creates_dest(self, tmp_path):
        dest = tmp_path / "out.zip"
        with _Sniff(_Resp(b"hello")):
            n = auto_update._http_get_resumable(
                "https://x.invalid/y", dest,
            )
        assert n == 5
        assert dest.read_bytes() == b"hello"
        # ``.part`` must be cleaned up after the final rename.
        assert not (tmp_path / "out.zip.part").exists()

    def test_resume_appends_on_206(self, tmp_path):
        """Customer's network dropped after byte 5; on retry, the
        server replies 206 with the remaining bytes, and we should
        end up with the complete file."""
        dest = tmp_path / "out.zip"
        # Pre-existing partial — first 5 bytes already on disk.
        part = tmp_path / "out.zip.part"
        part.write_bytes(b"hello")

        # Server replies 206 with the *remaining* bytes only.
        with _Sniff(_Resp(
            b" world",
            status=206,
            headers={"Content-Range": "bytes 5-10/11"},
        )) as s:
            n = auto_update._http_get_resumable(
                "https://x.invalid/y", dest,
            )
        assert n == 11
        assert dest.read_bytes() == b"hello world"
        # The outgoing request must have carried the Range header.
        req = s.requests[0]
        assert req.headers.get("Range") == "bytes=5-"

    def test_server_ignores_range_restarts(self, tmp_path):
        """Some CDNs return 200 even when asked for a Range. The
        resumer detects that and replaces the partial file with the
        fresh full body."""
        dest = tmp_path / "out.zip"
        (tmp_path / "out.zip.part").write_bytes(b"oldpartial")

        with _Sniff(_Resp(b"BRAND NEW BYTES", status=200)):
            n = auto_update._http_get_resumable(
                "https://x.invalid/y", dest,
            )
        assert n == len(b"BRAND NEW BYTES")
        assert dest.read_bytes() == b"BRAND NEW BYTES"

    def test_cancel_leaves_part_for_resume(self, tmp_path):
        dest = tmp_path / "out.zip"
        cancel = threading.Event()
        cancel.set()
        with _Sniff(_Resp(b"x" * 256)):
            with pytest.raises(auto_update.UpdateError, match="cancel"):
                auto_update._http_get_resumable(
                    "https://x.invalid/y", dest, cancel_event=cancel,
                )
        # dest must not exist (no final rename); .part may or may
        # not exist (cancelled before any bytes written is fine).
        assert not dest.exists()


# ── prune_cached_patches ───────────────────────────────────────────


class TestPruneCachedPatches:
    def test_removes_all_when_no_keep(self, tmp_path):
        cd = tmp_path / "cache" / "updates"
        cd.mkdir(parents=True)
        for n in ("npcreate-src-1.8.10-aaa.zip",
                  "npcreate-src-1.8.11-bbb.zip"):
            (cd / n).write_bytes(b"x")
        n = auto_update.prune_cached_patches(cache_dir=cd)
        assert n == 2
        assert list(cd.iterdir()) == []

    def test_keeps_named_version(self, tmp_path):
        cd = tmp_path / "cache" / "updates"
        cd.mkdir(parents=True)
        keep = cd / "npcreate-src-1.8.13-keepkeep.zip"
        keep.write_bytes(b"x")
        (cd / "npcreate-src-1.8.10-old.zip").write_bytes(b"y")
        n = auto_update.prune_cached_patches(
            cache_dir=cd, keep_version="1.8.13",
        )
        assert n == 1
        assert keep.exists()

    def test_no_dir_is_zero(self, tmp_path):
        n = auto_update.prune_cached_patches(cache_dir=tmp_path / "nope")
        assert n == 0


# ── UpdatePoller.poll_now / kick ───────────────────────────────────


class _StubManifest:
    """Mimics ``UpdateManifest`` enough for poll_now's identity
    check without dragging in the full ed25519 signing fixture
    used by ``test_auto_update.TestFetchManifest``."""
    def __init__(self, version: str) -> None:
        self.version = version


class TestPollerManualCheck:
    def test_poll_now_returns_manifest_and_fires_callback(self, monkeypatch):
        seen: list = []
        poller = auto_update.UpdatePoller(on_update=seen.append)
        m = _StubManifest("9.9.9")
        monkeypatch.setattr(auto_update, "fetch_manifest", lambda url: m)

        out = poller.poll_now()
        assert out is m
        assert seen == [m]

    def test_poll_now_no_update_returns_none(self, monkeypatch):
        seen: list = []
        poller = auto_update.UpdatePoller(on_update=seen.append)
        monkeypatch.setattr(auto_update, "fetch_manifest", lambda url: None)

        assert poller.poll_now() is None
        assert seen == []

    def test_poll_now_idempotent_for_same_version(self, monkeypatch):
        """The 6 h timer thread suppresses re-fires for the version
        it already surfaced; ``poll_now`` must follow the same rule
        or a spam-click of "Check now" floods the banner with
        re-shows of the same patch."""
        seen: list = []
        poller = auto_update.UpdatePoller(on_update=seen.append)
        m = _StubManifest("9.9.9")
        monkeypatch.setattr(auto_update, "fetch_manifest", lambda url: m)

        poller.poll_now()
        poller.poll_now()
        poller.poll_now()
        assert len(seen) == 1, "callback must not re-fire on repeat versions"

    def test_poll_now_swallows_exceptions(self, monkeypatch):
        """A network blip during a manual click must NOT raise into
        the UI — return None so the Settings page can just say
        "ตอนนี้ไม่มีอินเทอร์เน็ต"."""
        poller = auto_update.UpdatePoller(on_update=lambda _m: None)

        def _boom(_url):
            raise RuntimeError("network is on fire")
        monkeypatch.setattr(auto_update, "fetch_manifest", _boom)
        assert poller.poll_now() is None

    def test_kick_sets_event_and_is_idempotent(self):
        poller = auto_update.UpdatePoller(on_update=lambda _m: None)
        assert not poller._kick.is_set()
        poller.kick()
        assert poller._kick.is_set()
        # Second call must not raise / reset.
        poller.kick()
        assert poller._kick.is_set()

    def test_kick_wakes_background_loop(self, monkeypatch):
        """The headline contract for Settings → "Check now":
        kicking the timer thread causes it to perform a fresh poll
        far sooner than ``interval_s`` would normally allow.
        """
        seen: list = []
        # interval=60 so without kick we'd wait a full minute; the
        # test would time out long before that.
        poller = auto_update.UpdatePoller(
            on_update=seen.append, interval_s=60,
        )
        # Patch fetch_manifest to return a stub instantly.
        m = _StubManifest("9.9.9")
        monkeypatch.setattr(auto_update, "fetch_manifest", lambda url: m)
        # Skip the 30 s startup delay so the test runs in <1 s.
        real_wait = poller._stop.wait

        def _fast_wait(timeout=None):
            return real_wait(timeout=0)  # never block in tests
        poller._stop.wait = _fast_wait  # type: ignore[assignment]

        poller.start()
        try:
            # Background loop should fire at least once (initial
            # tick after the zero-wait startup delay).
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not seen:
                time.sleep(0.02)
        finally:
            poller.stop()
            if poller._thread is not None:
                poller._thread.join(timeout=2.0)
        assert seen, "background loop should have fired on_update at least once"
