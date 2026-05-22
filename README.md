# Pilot Protocol — Python SDK

[![PyPI version](https://img.shields.io/pypi/v/pilotprotocol)](https://pypi.org/project/pilotprotocol/)
[![Python versions](https://img.shields.io/pypi/pyversions/pilotprotocol)](https://pypi.org/project/pilotprotocol/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

Python client for the [Pilot Protocol](https://pilotprotocol.network) overlay network. Gives AI agents and services permanent addresses, encrypted peer-to-peer channels, and a mutual-trust model.

The SDK calls into a pre-built `libpilot` shared library (`.so` / `.dylib` / `.dll`) via `ctypes` and talks to a local `pilot-daemon` over a Unix domain socket.

## Install

```bash
pip install pilotprotocol
```

The wheel ships the native `libpilot` library plus console entry points: `pilotctl`, `pilot-daemon`, `pilot-gateway`, `pilot-updater`.

Supported platforms: Linux (x86_64, arm64), macOS (Intel, Apple Silicon). Windows is experimental.

## Quick start

Make sure a daemon is running:

```bash
pilotctl daemon start --hostname my-agent
```

Then, from your code:

```python
from pilotprotocol import Driver

with Driver() as d:
    info = d.info()
    print(f"address={info['address']}")

    d.set_hostname("my-python-agent")

    peer = d.resolve_hostname("other-agent")
    with d.dial(f"{peer['address']}:1000") as conn:
        conn.write(b"hello")
        print(conn.read(4096))
```

## API overview

`Driver` is the connection to the local daemon. Highlights:

- Identity: `info`, `set_hostname`, `set_visibility`, `set_tags`, `resolve_hostname`
- Trust: `handshake`, `pending_handshakes`, `approve_handshake`, `reject_handshake`, `trusted_peers`, `revoke_trust`
- Streams: `dial`, `listen` (returning `Conn` / `Listener`, both context managers)
- Datagrams: `send_to`, `recv_from`
- Built-in services: `send_message`, `send_file` (data exchange); `publish_event`, `subscribe_event` (event stream); `submit_task` (task queue)
- Config: `set_webhook`, `set_task_exec`, `deregister`, `disconnect`

All daemon-side errors are raised as `PilotError`.

```python
from pilotprotocol import Driver, PilotError

try:
    with Driver() as d:
        d.resolve_hostname("unknown")
except PilotError as e:
    print(f"error: {e}")
```

## Native library lookup

The SDK searches for `libpilot.{so,dylib,dll}` in this order:

1. `PILOT_LIB_PATH` environment variable (explicit path)
2. The installed package directory (bundled wheel layout)
3. `~/.pilot/bin/` (entry-point install location)
4. `<project_root>/bin/` (development layout)
5. System library search path

## Examples

See `examples/` for runnable programs:

- `basic_usage.py` — connection, identity, trust
- `data_exchange_demo.py` — messages, files, JSON
- `event_stream_demo.py` — pub/sub
- `task_submit_demo.py` — task delegation
- `pydantic_ai_agent.py` / `pydantic_ai_multiagent.py` — PydanticAI integration

## Testing

```bash
python -m pytest tests/ -v
```

## Links

- Homepage: <https://pilotprotocol.network>
- Issues: <https://github.com/pilot-protocol/sdk-python/issues>
- Node.js SDK: [`pilotprotocol`](https://www.npmjs.com/package/pilotprotocol) on npm
- Swift SDK: [`sdk-swift`](https://github.com/pilot-protocol/sdk-swift)

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
