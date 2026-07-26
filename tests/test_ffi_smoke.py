"""Non-mocked FFI smoke test.

Every other test in this suite substitutes a fake library object, so a
binding that names a symbol libpilot no longer exports still passes. This
module loads a real ``libpilot`` shared library and resolves every symbol
the SDK declares, which is what catches drift between the two repos.

Library discovery order:
  1. ``PILOT_LIB_PATH``
  2. a sibling ``libpilot/`` checkout next to this repo
  3. ``~/.pilot/bin/``

If no library is found the module skips, unless ``PILOT_REQUIRE_LIB=1`` is
set (CI sets it after building libpilot from source, so a missing build is
a failure there rather than a silent skip).
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
from pathlib import Path

import pytest

from pilotprotocol import client as client_mod

_LIB_NAMES = {
    "Darwin": "libpilot.dylib",
    "Linux": "libpilot.so",
    "Windows": "libpilot.dll",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _candidate_paths() -> list[Path]:
    lib_name = _LIB_NAMES.get(platform.system())
    if lib_name is None:
        return []
    env = os.environ.get("PILOT_LIB_PATH")
    if env:
        return [Path(env)]
    return [
        _REPO_ROOT.parent / "libpilot" / lib_name,
        _REPO_ROOT.parent / "libpilot" / "libpilot" / lib_name,
        Path.home() / ".pilot" / "bin" / lib_name,
    ]


def _locate_library() -> Path | None:
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


def _required_symbols() -> set[str]:
    """Every libpilot symbol this SDK names, read off the SDK source.

    Two call styles have to be covered: attributes touched directly in
    ``_setup_signatures`` (``lib.PilotFoo``) and names handed to
    ``_call_json`` as string literals (resolved by ``getattr`` at call time).
    """
    src = Path(client_mod.__file__).read_text()
    names = set(re.findall(r"\blib\.(Pilot[A-Za-z0-9]+|FreeString)\b", src))
    # Names iterated as string literals inside _setup_signatures loops and
    # every _call_json("PilotFoo", ...) call site.
    names |= set(re.findall(r'"(Pilot[A-Za-z0-9]+)"', src))
    # PilotError is the SDK's own exception type, not a library symbol.
    names.discard("PilotError")
    return names


@pytest.fixture(scope="module")
def real_lib() -> ctypes.CDLL:
    path = _locate_library()
    if path is None:
        msg = (
            "libpilot shared library not found; set PILOT_LIB_PATH or build "
            "it in a sibling libpilot/ checkout"
        )
        if os.environ.get("PILOT_REQUIRE_LIB") == "1":
            pytest.fail(msg)
        pytest.skip(msg)
    return ctypes.CDLL(str(path))


def test_library_loads(real_lib: ctypes.CDLL) -> None:
    assert real_lib is not None


def test_setup_signatures_resolves_every_binding(real_lib: ctypes.CDLL) -> None:
    """``_setup_signatures`` must not name a symbol the library lacks."""
    client_mod._setup_signatures(real_lib)


def test_every_named_symbol_is_exported(real_lib: ctypes.CDLL) -> None:
    """No symbol named anywhere in client.py is missing from the library."""
    missing = []
    for name in sorted(_required_symbols()):
        try:
            getattr(real_lib, name)
        except AttributeError:
            missing.append(name)
    assert not missing, f"client.py names symbols libpilot does not export: {missing}"


def test_get_lib_succeeds_against_real_library(
    monkeypatch: pytest.MonkeyPatch, real_lib: ctypes.CDLL
) -> None:
    """The public loader path works end to end against the real library."""
    path = _locate_library()
    assert path is not None
    monkeypatch.setattr(client_mod, "_lib", None)
    monkeypatch.setenv("PILOT_LIB_PATH", str(path))
    lib = client_mod._get_lib()
    assert lib is not None
    monkeypatch.setattr(client_mod, "_lib", None)
