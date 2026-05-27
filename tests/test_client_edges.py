"""Small additional edge-case tests for pilotprotocol.client helpers.

Targets the leftover branches not covered by test_client.py:
- ``_find_library`` ~/.pilot/bin lookup path
- ``_void_ptr_to_bytes`` null + non-null branches
- ``_free`` null + non-null branches
- ``Conn.read`` size <= 0 and size > 16 MB cap
- ``__init__`` ``__version__`` import-failure fallback
"""

from __future__ import annotations

import ctypes
import platform
import types
from pathlib import Path
from unittest import mock

import pytest

import pilotprotocol.client as client_mod


# ---------------------------------------------------------------------------
# _find_library: ~/.pilot/bin/<libname> branch
# ---------------------------------------------------------------------------


class TestFindLibraryPilotBin:
    def test_returns_home_pilot_bin_path(self, tmp_path, monkeypatch):
        # Build a fake home with ~/.pilot/bin/<libname>
        fake_home = tmp_path / "home"
        pilot_bin = fake_home / ".pilot" / "bin"
        pilot_bin.mkdir(parents=True)
        lib_name = client_mod._LIB_NAMES[platform.system()]
        lib_file = pilot_bin / lib_name
        lib_file.write_bytes(b"\x7fELF\x00\x00\x00\x00")  # whatever
        monkeypatch.delenv("PILOT_LIB_PATH", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        result = client_mod._find_library()
        assert result == str(lib_file)


# ---------------------------------------------------------------------------
# _void_ptr_to_bytes
# ---------------------------------------------------------------------------


class TestVoidPtrToBytes:
    def test_null_returns_none(self):
        assert client_mod._void_ptr_to_bytes(None) is None
        assert client_mod._void_ptr_to_bytes(0) is None

    def test_nonnull_reads_c_string(self):
        buf = ctypes.create_string_buffer(b"hello\x00")
        ptr = ctypes.cast(buf, ctypes.c_void_p).value
        result = client_mod._void_ptr_to_bytes(ptr)
        assert result == b"hello"


# ---------------------------------------------------------------------------
# _free
# ---------------------------------------------------------------------------


class TestFree:
    def test_null_is_noop(self, monkeypatch):
        # Should not even call FreeString.
        sentinel = {"called": False}

        class FakeLib:
            def FreeString(self, ptr):
                sentinel["called"] = True

        monkeypatch.setattr(client_mod, "_get_lib", lambda: FakeLib())
        client_mod._free(None)
        client_mod._free(0)
        assert sentinel["called"] is False

    def test_nonnull_calls_free_string(self, monkeypatch):
        sentinel = {"freed": []}

        class FakeLib:
            def FreeString(self, ptr):
                sentinel["freed"].append(ptr)

        monkeypatch.setattr(client_mod, "_get_lib", lambda: FakeLib())
        client_mod._free(123)
        assert sentinel["freed"] == [123]


# ---------------------------------------------------------------------------
# Conn.read size bounds
# ---------------------------------------------------------------------------


class _FakeReadLib:
    """Captures the size argument passed to PilotConnRead."""

    def __init__(self):
        self.last_size = None

    def FreeString(self, ptr):
        pass

    def PilotConnRead(self, h, size):
        self.last_size = size
        return types.SimpleNamespace(n=0, data=None, err=None)


class TestConnReadSizeBounds:
    def test_zero_or_negative_returns_empty_without_call(self, monkeypatch):
        lib = _FakeReadLib()
        monkeypatch.setattr(client_mod, "_get_lib", lambda: lib)
        conn = client_mod.Conn(handle=10)
        assert conn.read(0) == b""
        assert conn.read(-100) == b""
        # Library was never invoked.
        assert lib.last_size is None

    def test_size_over_16mb_is_capped(self, monkeypatch):
        lib = _FakeReadLib()
        monkeypatch.setattr(client_mod, "_get_lib", lambda: lib)
        conn = client_mod.Conn(handle=10)
        conn.read(64 * 1024 * 1024)
        assert lib.last_size == 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# __init__ version fallback
# ---------------------------------------------------------------------------


class TestInitVersionFallback:
    def test_version_resolves_to_string(self):
        # Just verify the module imports and exposes a string __version__.
        # The "unknown" fallback path requires importlib.metadata.version to
        # raise; we can simulate that by reloading the module with a stub.
        import importlib
        import importlib.metadata as md
        import pilotprotocol as pp

        assert isinstance(pp.__version__, str)
        assert pp.__version__  # non-empty

    def test_version_fallback_when_metadata_missing(self, monkeypatch):
        # Reimport pilotprotocol with importlib.metadata.version raising.
        import importlib
        import importlib.metadata as md
        import sys

        def boom(name):
            raise md.PackageNotFoundError(name)

        monkeypatch.setattr(md, "version", boom)
        # Force a reimport of the top-level package.
        if "pilotprotocol" in sys.modules:
            del sys.modules["pilotprotocol"]
        try:
            import pilotprotocol as pp_reimport
            assert pp_reimport.__version__ == "unknown"
        finally:
            # Restore the original module so other tests see the real version.
            if "pilotprotocol" in sys.modules:
                del sys.modules["pilotprotocol"]
            import pilotprotocol  # noqa: F401
