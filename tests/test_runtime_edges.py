"""Edge-case tests for pilotprotocol._runtime.

The main test_runtime.py file covers the happy paths and the documented
state machine; this file targets the small branches the main suite skips:

- ``_bundled_version`` / ``_runtime_version`` failure paths
- ``_daemon_running`` config-load + socket-close errors
- ``_atomic_install`` rename-failure cleanup
- ``_ensure_dir_writable`` permission failure
- ``_ensure_default_config`` race
- ``run_seeder`` ETXTBSY skip + non-busy OSError reraise
- ``runtime_binary`` / ``runtime_library`` fallback to the wheel
- The unsupported-platform branch in ``_platform_lib_name``
- ``ensure_runtime_seeded`` cached-true return path
"""

from __future__ import annotations

import errno
import json
import os
import platform as platform_mod
import socket
from pathlib import Path

import pytest

import pilotprotocol._runtime as rt


# ---------------------------------------------------------------------------
# Shared isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test starts with a clean tmp PILOT_HOME and reset marker."""
    fake_home = tmp_path / "home" / ".pilot"
    monkeypatch.setenv("PILOT_HOME", str(fake_home))
    monkeypatch.setattr(rt, "_daemon_running", lambda: False)
    rt.reset_seeded_marker()
    yield {"home": fake_home, "tmp": tmp_path, "monkeypatch": monkeypatch}
    rt.reset_seeded_marker()


# ---------------------------------------------------------------------------
# _platform_lib_name
# ---------------------------------------------------------------------------


class TestPlatformLibName:
    def test_unsupported_platform_raises(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "system", lambda: "Plan9")
        with pytest.raises(OSError, match="unsupported platform"):
            rt._platform_lib_name()


# ---------------------------------------------------------------------------
# _pkg_bin_dir — make sure the un-stubbed code path is exercised
# ---------------------------------------------------------------------------


class TestPkgBinDir:
    def test_returns_real_bin_dir_next_to_module(self):
        p = rt._pkg_bin_dir()
        assert isinstance(p, Path)
        assert p.name == "bin"
        # Anchored at the runtime module directory.
        assert p.parent == Path(rt.__file__).resolve().parent


# ---------------------------------------------------------------------------
# _runtime_root — both branches
# ---------------------------------------------------------------------------


class TestRuntimeRoot:
    def test_without_pilot_home_uses_home_dot_pilot(self, tmp_path, monkeypatch):
        # The autouse fixture sets PILOT_HOME — undo it.
        monkeypatch.delenv("PILOT_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = rt._runtime_root()
        assert result == tmp_path / ".pilot"

    def test_with_pilot_home_uses_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PILOT_HOME", str(tmp_path / "override"))
        result = rt._runtime_root()
        assert result == tmp_path / "override"


# ---------------------------------------------------------------------------
# _bundled_version / _runtime_version
# ---------------------------------------------------------------------------


class TestBundledVersion:
    def test_read_failure_falls_through_to_metadata(self, tmp_path, monkeypatch):
        # Create a marker file then make read_text raise.
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / ".pilot-version").write_text("9.9.9\n")
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        # Override read_text to raise OSError → fall through to importlib.metadata.
        orig_read = Path.read_text

        def boom(self, *a, **kw):
            if self.name == ".pilot-version":
                raise OSError("io")
            return orig_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        v = rt._bundled_version()
        # importlib.metadata path returns the installed package version,
        # which exists in this venv. Just assert non-empty fallback.
        assert isinstance(v, str)

    def test_no_marker_no_metadata_returns_empty(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg-no-marker"
        pkg.mkdir()
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        # Make importlib.metadata.version raise.
        import importlib.metadata as md

        def boom(_name):
            raise md.PackageNotFoundError("missing")

        monkeypatch.setattr(md, "version", boom)
        assert rt._bundled_version() == ""

    def test_runtime_version_read_failure(self, tmp_path, monkeypatch):
        rtdir = tmp_path / "rt"
        rtdir.mkdir()
        (rtdir / ".pilot-version").write_text("1.0.0\n")

        orig = Path.read_text

        def boom(self, *a, **kw):
            if self.name == ".pilot-version":
                raise OSError("denied")
            return orig(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        assert rt._runtime_version(rtdir) == ""


# ---------------------------------------------------------------------------
# _semver_tuple edge cases (already mostly covered, but a few values left)
# ---------------------------------------------------------------------------


class TestSemverTuple:
    def test_empty_returns_empty_tuple(self):
        assert rt._semver_tuple("") == ()
        assert rt._semver_tuple(None) == ()

    def test_nonnumeric_returns_empty_tuple(self):
        assert rt._semver_tuple("not-a-version") == ()

    def test_strips_leading_v_and_suffixes(self):
        assert rt._semver_tuple("v1.2.3-beta+build") == (1, 2, 3)


# ---------------------------------------------------------------------------
# _daemon_running edge paths
# ---------------------------------------------------------------------------


class TestDaemonRunning:
    def test_no_config_no_socket(self, tmp_path, monkeypatch):
        # The autouse fixture stubs _daemon_running. Reach the real function.
        monkeypatch.undo()
        monkeypatch.setenv("PILOT_HOME", str(tmp_path / ".pilot"))
        assert rt._daemon_running() is False

    def test_config_unreadable_uses_default_socket(self, tmp_path, monkeypatch):
        monkeypatch.undo()
        home = tmp_path / ".pilot"
        home.mkdir()
        # Bad JSON triggers ValueError branch.
        (home / "config.json").write_text("not json {{{")
        monkeypatch.setenv("PILOT_HOME", str(home))
        # No socket exists → returns False without error.
        assert rt._daemon_running() is False

    def test_socket_present_but_connect_fails(self, tmp_path, monkeypatch):
        monkeypatch.undo()
        home = tmp_path / ".pilot"
        home.mkdir()
        sock_path = tmp_path / "pilot.sock"
        # Make a regular file that exists at sock_path so Path.exists() is True
        # but connect() fails.
        sock_path.write_text("not a socket")
        (home / "config.json").write_text(json.dumps({"socket": str(sock_path)}))
        monkeypatch.setenv("PILOT_HOME", str(home))
        assert rt._daemon_running() is False

    def test_socket_close_exception_swallowed(self, tmp_path, monkeypatch):
        """Lines 143-144: if s.close() raises in the finally, swallow it."""
        monkeypatch.undo()
        home = tmp_path / ".pilot"
        home.mkdir()
        monkeypatch.setenv("PILOT_HOME", str(home))

        # Stub socket.socket so close() raises but exists() / connect() can
        # be controlled. The path also needs to not exist so we return early
        # — actually we need to reach the finally branch, which requires
        # the socket path to exist. Use a real file at sock_path and force
        # close() to raise.
        sock_path = tmp_path / "fake.sock"
        sock_path.write_text("x")  # exists() True
        (home / "config.json").write_text(
            json.dumps({"socket": str(sock_path)})
        )

        class BadCloseSocket:
            def __init__(self, *a, **kw):
                pass
            def settimeout(self, t):
                pass
            def connect(self, path):
                raise OSError("can't connect to real file")
            def close(self):
                raise OSError("close blew up")

        monkeypatch.setattr(rt.socket, "socket", BadCloseSocket)
        # Should not raise — the finally swallows the close error.
        result = rt._daemon_running()
        assert result is False

    def test_socket_connect_succeeds(self, tmp_path, monkeypatch):
        # macOS AF_UNIX limit is ~104 bytes. Use a short /tmp path instead
        # of tmp_path which can exceed it under pytest.
        import tempfile
        monkeypatch.undo()
        home = tmp_path / ".pilot"
        home.mkdir()
        with tempfile.TemporaryDirectory(prefix="psk-") as short:
            sock_path = Path(short) / "p.sock"
            srv = socket.socket(socket.AF_UNIX)
            try:
                srv.bind(str(sock_path))
                srv.listen(1)
                (home / "config.json").write_text(
                    json.dumps({"socket": str(sock_path)})
                )
                monkeypatch.setenv("PILOT_HOME", str(home))
                assert rt._daemon_running() is True
            finally:
                srv.close()
                if sock_path.exists():
                    sock_path.unlink()


# ---------------------------------------------------------------------------
# _atomic_install
# ---------------------------------------------------------------------------


class TestAtomicInstall:
    def test_happy_path(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dst = tmp_path / "dst.bin"
        rt._atomic_install(src, dst)
        assert dst.read_bytes() == b"payload"
        assert dst.stat().st_mode & 0o777 == 0o755

    def test_preexisting_tmp_is_cleared(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"v1")
        dst = tmp_path / "dst.bin"
        # Predict the tmp name our function will pick.
        import threading
        tmp_name = f"dst.bin.tmp.{os.getpid()}.{threading.get_ident()}"
        (tmp_path / tmp_name).write_text("stale")
        rt._atomic_install(src, dst)
        assert dst.read_bytes() == b"v1"

    def test_replace_failure_cleans_tmp(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        dst = tmp_path / "dst.bin"

        # Force os.replace to raise once.
        original_replace = os.replace
        calls = {"n": 0}

        def boom(a, b):
            calls["n"] += 1
            raise OSError(errno.EACCES, "denied")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            rt._atomic_install(src, dst)
        # tmp should be gone (cleanup ran)
        import threading
        tmp = tmp_path / f"dst.bin.tmp.{os.getpid()}.{threading.get_ident()}"
        assert not tmp.exists()

    def test_replace_failure_tmp_already_gone(self, tmp_path, monkeypatch):
        """If the cleanup unlink also fails, the original OSError still bubbles."""
        src = tmp_path / "src.bin"
        src.write_bytes(b"x")
        dst = tmp_path / "dst.bin"

        def boom_replace(a, b):
            raise OSError(errno.EACCES, "denied")

        original_unlink = Path.unlink

        def boom_unlink(self, *a, **kw):
            raise OSError(errno.ENOENT, "gone")

        monkeypatch.setattr(os, "replace", boom_replace)
        monkeypatch.setattr(Path, "unlink", boom_unlink)
        with pytest.raises(OSError):
            rt._atomic_install(src, dst)


# ---------------------------------------------------------------------------
# _ensure_dir_writable
# ---------------------------------------------------------------------------


class TestEnsureDirWritable:
    def test_creates_missing_dir(self, tmp_path):
        p = tmp_path / "deep" / "nested" / "dir"
        rt._ensure_dir_writable(p)
        assert p.is_dir()

    def test_unwritable_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "ro"
        p.mkdir()
        # Pretend it's not writable.
        monkeypatch.setattr(os, "access", lambda path, mode: False)
        with pytest.raises(PermissionError, match="not writable"):
            rt._ensure_dir_writable(p)


# ---------------------------------------------------------------------------
# _ensure_default_config race
# ---------------------------------------------------------------------------


class TestEnsureDefaultConfigRace:
    def test_handles_race_with_other_writer(self, tmp_path, monkeypatch):
        # Simulate another writer winning the os.replace race
        # (FileNotFoundError branch).
        monkeypatch.setenv("PILOT_HOME", str(tmp_path / ".pilot"))

        original_replace = os.replace
        calls = {"n": 0}

        def racing_replace(a, b):
            calls["n"] += 1
            raise FileNotFoundError(2, "no such file")

        monkeypatch.setattr(os, "replace", racing_replace)
        # Should not raise even though replace failed.
        rt._ensure_default_config()

    def test_handles_race_when_tmp_already_unlinked(self, tmp_path, monkeypatch):
        """tmp.unlink raises inside cleanup — must not propagate."""
        monkeypatch.setenv("PILOT_HOME", str(tmp_path / ".pilot"))

        def racing_replace(a, b):
            raise FileNotFoundError(2, "no such file")

        def cleanup_fails(self):
            raise OSError(errno.ENOENT, "double race")

        monkeypatch.setattr(os, "replace", racing_replace)
        monkeypatch.setattr(Path, "unlink", cleanup_fails)
        # Should still return without raising.
        rt._ensure_default_config()


# ---------------------------------------------------------------------------
# run_seeder OSError paths
# ---------------------------------------------------------------------------


class TestSeederOSErrorPaths:
    def test_etxtbsy_during_copy_skips(self, tmp_path, monkeypatch):
        # Build a fake pkg/bin
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        names = list(rt._BIN_NAMES) + [rt._platform_lib_name()]
        for n in names:
            (pkg / n).write_text("stub")
        (pkg / ".pilot-version").write_text("1.0.0\n")
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        # Make _atomic_install raise ETXTBSY for the first name copied.
        seen = {"hit": False}
        original = rt._atomic_install

        def flaky(src, dst):
            if not seen["hit"]:
                seen["hit"] = True
                raise OSError(errno.ETXTBSY, "busy")
            original(src, dst)

        monkeypatch.setattr(rt, "_atomic_install", flaky)
        report = rt.run_seeder()
        assert seen["hit"] is True
        assert len(report.skipped) >= 1  # at least the busy one was skipped

    def test_other_oserror_propagates(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        for n in list(rt._BIN_NAMES) + [rt._platform_lib_name()]:
            (pkg / n).write_text("stub")
        (pkg / ".pilot-version").write_text("1.0.0\n")
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        def boom(src, dst):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(rt, "_atomic_install", boom)
        with pytest.raises(OSError):
            rt.run_seeder()


# ---------------------------------------------------------------------------
# runtime_binary / runtime_library fallback paths
# ---------------------------------------------------------------------------


class TestRuntimeBinaryFallback:
    def test_falls_back_to_wheel_when_rt_missing(self, tmp_path, monkeypatch):
        # Build a fake pkg with only one binary present.
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "pilotctl").write_text("from-wheel")
        (pkg / "pilotctl").chmod(0o755)
        (pkg / ".pilot-version").write_text("1.0.0\n")
        # libpilot is required by the platform_lib check inside run_seeder.
        # Without it the version-marker comparison can decide to skip copy,
        # but we want the SEED action to do nothing for our target name —
        # simplest way is to bypass the seeder entirely.
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)
        monkeypatch.setattr(rt, "ensure_runtime_seeded", lambda: tmp_path / "empty-rt")
        (tmp_path / "empty-rt").mkdir()

        p = rt.runtime_binary("pilotctl")
        assert p == pkg / "pilotctl"

    def test_missing_in_both_raises(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)
        monkeypatch.setattr(rt, "ensure_runtime_seeded", lambda: tmp_path / "empty-rt")
        (tmp_path / "empty-rt").mkdir()

        with pytest.raises(FileNotFoundError, match="not found"):
            rt.runtime_binary("does-not-exist")


class TestRuntimeLibraryFallback:
    def test_present_in_rt_returns_rt(self, tmp_path, monkeypatch):
        rtdir = tmp_path / "rt"
        rtdir.mkdir()
        libname = rt._platform_lib_name()
        (rtdir / libname).write_text("lib")
        monkeypatch.setattr(rt, "ensure_runtime_seeded", lambda: rtdir)
        # _pkg_bin_dir doesn't matter for this branch.
        assert rt.runtime_library() == rtdir / libname

    def test_falls_back_to_wheel(self, tmp_path, monkeypatch):
        # RT empty, wheel has it.
        rtdir = tmp_path / "rt"
        rtdir.mkdir()
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        libname = rt._platform_lib_name()
        (pkg / libname).write_text("lib")
        monkeypatch.setattr(rt, "ensure_runtime_seeded", lambda: rtdir)
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)
        assert rt.runtime_library() == pkg / libname

    def test_missing_everywhere_raises(self, tmp_path, monkeypatch):
        rtdir = tmp_path / "rt"
        rtdir.mkdir()
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        monkeypatch.setattr(rt, "ensure_runtime_seeded", lambda: rtdir)
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)
        with pytest.raises(FileNotFoundError, match="libpilot"):
            rt.runtime_library()


# ---------------------------------------------------------------------------
# ensure_runtime_seeded cached path
# ---------------------------------------------------------------------------


class TestEnsureRuntimeSeededCache:
    def test_second_call_returns_without_re_seeding(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        for n in list(rt._BIN_NAMES) + [rt._platform_lib_name()]:
            (pkg / n).write_text("stub")
        (pkg / ".pilot-version").write_text("1.0.0\n")
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        first = rt.ensure_runtime_seeded()
        # Now make run_seeder raise — if cache works, it never runs.
        monkeypatch.setattr(rt, "run_seeder", lambda: (_ for _ in ()).throw(
            RuntimeError("should not be called")))
        second = rt.ensure_runtime_seeded()
        assert second == first

    def test_force_bypasses_cache(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        for n in list(rt._BIN_NAMES) + [rt._platform_lib_name()]:
            (pkg / n).write_text("stub")
        (pkg / ".pilot-version").write_text("1.0.0\n")
        monkeypatch.setattr(rt, "_pkg_bin_dir", lambda: pkg)

        rt.ensure_runtime_seeded()
        calls = {"n": 0}
        original = rt.run_seeder

        def counting():
            calls["n"] += 1
            return original()

        monkeypatch.setattr(rt, "run_seeder", counting)
        rt.ensure_runtime_seeded(force=True)
        assert calls["n"] == 1
