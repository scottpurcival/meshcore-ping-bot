# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python bot that connects to a MeshCore companion radio node over BLE, logs all private (direct) messages and channel messages it observes, and auto-replies `pong` to any private message containing `ping`. It's meant to run as a long-lived foreground process on a Windows machine (no service/daemon wrapper).

## Commands

Setup (Windows, PowerShell/cmd):
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Run the bot:
```
.venv\Scripts\python.exe pingpong_bot.py
```
or double-click `run_bot.bat` (which does the same and pauses on exit so the window doesn't vanish on a crash).

List nearby BLE devices (name, address, signal strength) to find the right `address` for `config.json`:
```
.venv\Scripts\python.exe scan_ble.py
```

There is no lint, test, or build tooling in this repo — verification is done by running the script against real hardware and reading the console log. `python -m py_compile pingpong_bot.py` is the closest thing to a sanity check available.

## Configuration

`config.json` (not committed logic, just data — edit directly):
- `address`: BLE MAC address of the companion device (e.g. `"ED:13:DC:54:6E:47"`), or `null` to auto-scan for a device whose advertised name starts with `MeshCore`. Prefer setting an explicit address once known — it's more reliable than scanning, especially with multiple MeshCore devices nearby.
- `pin`: any non-null value triggers a BLE pairing attempt on connect. The actual string value is discarded by the underlying library/Windows — it's only used as an on/off flag for whether to call `pair()`.

## Architecture

Everything lives in `pingpong_bot.py`, built on the `meshcore` PyPI package (async, event-driven bindings for MeshCore companion firmware — repo: meshcore-dev/meshcore_py). Key facts about that library that shape this code and matter for future changes:

- **Connection**: `MeshCore.create_ble(address, pin)` returns `None` on failure rather than raising in most paths (`connect()` itself raises `ConnectionError` in the one case where it does fail). The connected instance's BLE address is at `meshcore.cx.connection.address` (`cx` is a `ConnectionManager`, not the raw connection).
- **Receiving**: incoming private and channel messages arrive as `EventType.CONTACT_MSG_RECV` / `EventType.CHANNEL_MSG_RECV` events via `meshcore.subscribe(event_type, callback, attribute_filters=...)`, but only after `await meshcore.start_auto_message_fetching()` has been called to start the background poll loop — subscribing alone does nothing.
- **Message payloads**: `event.payload["text"]`, `["pubkey_prefix"]` (6-byte hex string, DMs) or `["channel_idx"]` (channels), and `["path_len"]` for hop count. `path_len == 255` is a sentinel meaning "direct/0-hop", not an error — see `format_hops()`.
- **Replying**: `meshcore.commands.send_msg(dst, text)` accepts the raw 6-byte `pubkey_prefix` hex string directly as `dst` — no need to resolve a full contact object first. `get_contact_by_key_prefix()` is only used here for a human-readable name in logs, not for routing.
- **Delivery confirmation**: `send_msg` only confirms the companion *queued* the message (`EventType.MSG_SENT`), not that it reached the recipient. Actual delivery requires matching `result.payload["expected_ack"]` against a subsequent `EventType.ACK` event's `code` attribute via `meshcore.wait_for_event(...)`. The ACK packet itself carries no hop-count field — the code reuses the *incoming* message's hop count as an approximation when logging ACKed sends (see comment in `handle_private_message`).
- **Channel names**: resolved on demand via `meshcore.commands.get_channel(channel_idx)` and cached in the local `channel_names` dict for the life of the process — there's no bulk "list all channels" call.
- **BLE pairing on Windows**: bleak's WinRT backend only supports "Just Works" pairing (see comment in `bleak/backends/winrt/client.py`), which isn't sufficient if the companion device demands authenticated/MITM pairing. If BLE connect fails with `Insufficient Authentication`, the fix is pairing the device through Windows' own Bluetooth settings UI first (which supports passkey/authenticated pairing), then letting this script connect to the already-bonded device.
