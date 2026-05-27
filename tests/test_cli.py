"""Tests for the CLI entry-point shims (pilotprotocol/cli.py).

The shims seed ``~/.pilot/bin/`` then exec the seeded binary. We replace
``ensure_runtime_seeded`` + ``runtime_binary`` + ``subprocess.call`` so no
real binaries are required.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import pilotprotocol.cli as cli_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path):
    """Stub ensure_runtime_seeded + runtime_binary so cli shims run dry."""
    rt_bin = tmp_path / "rt-bin"
    rt_bin.mkdir()

    seeded = {"called": 0}

    def fake_seed():
        seeded["called"] += 1
        return rt_bin

    def fake_binary(name: str) -> Path:
        p = rt_bin / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        return p

    monkeypatch.setattr(cli_mod, "ensure_runtime_seeded", fake_seed)
    monkeypatch.setattr(cli_mod, "runtime_binary", fake_binary)
    return {"rt": rt_bin, "seeded": seeded}


@pytest.fixture
def fake_call(monkeypatch):
    """Capture subprocess.call invocations and return a controlled exit code."""
    calls = []

    def _call(cmd):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(cli_mod.subprocess, "call", _call)
    return calls


# ---------------------------------------------------------------------------
# Each entry point should seed, then exec the right binary
# ---------------------------------------------------------------------------


class TestRunPilotctl:
    def test_seeds_and_exits_zero(self, fake_runtime, fake_call, monkeypatch):
        monkeypatch.setattr(cli_mod.sys, "argv", ["pilotctl", "info"])
        with pytest.raises(SystemExit) as exc:
            cli_mod.run_pilotctl()
        assert exc.value.code == 0
        assert fake_runtime["seeded"]["called"] == 1
        assert len(fake_call) == 1
        # Command should be [<rt>/pilotctl, "info"]
        cmd = fake_call[0]
        assert cmd[0].endswith("pilotctl")
        assert cmd[1:] == ["info"]

    def test_propagates_nonzero_exit(self, fake_runtime, monkeypatch):
        monkeypatch.setattr(cli_mod.subprocess, "call", lambda cmd: 7)
        monkeypatch.setattr(cli_mod.sys, "argv", ["pilotctl"])
        with pytest.raises(SystemExit) as exc:
            cli_mod.run_pilotctl()
        assert exc.value.code == 7

    def test_argv_passthrough_with_flags(self, fake_runtime, fake_call, monkeypatch):
        monkeypatch.setattr(
            cli_mod.sys, "argv",
            ["pilotctl", "send-message", "agent", "--data", "hi", "--wait"],
        )
        with pytest.raises(SystemExit):
            cli_mod.run_pilotctl()
        assert fake_call[0][1:] == [
            "send-message", "agent", "--data", "hi", "--wait",
        ]


class TestRunDaemon:
    def test_invokes_pilot_daemon(self, fake_runtime, fake_call, monkeypatch):
        monkeypatch.setattr(cli_mod.sys, "argv", ["pilot-daemon", "--email", "a@b"])
        with pytest.raises(SystemExit):
            cli_mod.run_daemon()
        assert fake_call[0][0].endswith("pilot-daemon")
        assert fake_call[0][1:] == ["--email", "a@b"]


class TestRunGateway:
    def test_invokes_pilot_gateway(self, fake_runtime, fake_call, monkeypatch):
        monkeypatch.setattr(cli_mod.sys, "argv", ["pilot-gateway"])
        with pytest.raises(SystemExit):
            cli_mod.run_gateway()
        assert fake_call[0][0].endswith("pilot-gateway")


class TestRunUpdater:
    def test_invokes_pilot_updater(self, fake_runtime, fake_call, monkeypatch):
        monkeypatch.setattr(cli_mod.sys, "argv", ["pilot-updater", "--check"])
        with pytest.raises(SystemExit):
            cli_mod.run_updater()
        assert fake_call[0][0].endswith("pilot-updater")
        assert fake_call[0][1:] == ["--check"]


# ---------------------------------------------------------------------------
# Imports / module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_all_entry_points_exist_and_are_callable(self):
        for name in ("run_pilotctl", "run_daemon", "run_gateway", "run_updater"):
            fn = getattr(cli_mod, name)
            assert callable(fn), f"{name} must be callable"

    def test_console_scripts_match_pyproject(self):
        # Sanity: the wrappers point at the names declared in pyproject.toml.
        # If the binary name list drifts, this test fails loudly.
        from pilotprotocol._runtime import _BIN_NAMES

        expected = {"pilotctl", "pilot-daemon", "pilot-gateway", "pilot-updater"}
        assert set(_BIN_NAMES) == expected
