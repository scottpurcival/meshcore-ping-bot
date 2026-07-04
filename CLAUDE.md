# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python bot that connects to a MeshCore companion radio node over BLE, logs all private (direct) messages and channel messages it observes, and auto-replies `pong` to any private message containing `ping`. It also runs an interactive `prompt_toolkit` prompt for sending messages/commands (`@Name text`, `@#channel text`, `/advert`, `/help`) by hand. It's meant to run as a long-lived foreground process on a Windows machine (no service/daemon wrapper).

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
- **Channel names**: there's no bulk "list all channels" call, so the bot probes indices `0..CHANNEL_PROBE_COUNT-1` via `meshcore.commands.get_channel(idx)` once at startup and builds both an idx→name (`channel_by_idx`, for logging received messages) and name→idx (`channel_by_name`, for resolving `@#name`/`@Public` input) map. Raise `CHANNEL_PROBE_COUNT` if a device has more channel slots configured.
- **Sending to a channel**: `meshcore.commands.send_chan_msg(channel_idx, text)` — unlike `send_msg`, it only returns `EventType.OK`/`ERROR`, no `expected_ack`/ACK to wait for, since a channel broadcast has no single recipient to confirm delivery.
- **Broadcasting presence**: `meshcore.commands.send_advert(flood=False)` — wired up as the `/advert` (and `/advert flood`) REPL command.
- **BLE pairing on Windows**: bleak's WinRT backend only supports "Just Works" pairing (see comment in `bleak/backends/winrt/client.py`), which isn't sufficient if the companion device demands authenticated/MITM pairing. If BLE connect fails with `Insufficient Authentication`, the fix is pairing the device through Windows' own Bluetooth settings UI first (which supports passkey/authenticated pairing), then letting this script connect to the already-bonded device.
- **Concurrent command race (important)**: `CommandHandlerBase.send()` in the library matches a command's response purely by `EventType` (e.g. `[MSG_SENT, ERROR]` for `send_msg`, `[CONTACT_MSG_RECV, CHANNEL_MSG_RECV, ERROR, NO_MORE_MSGS]` for the internal `get_msg()` the auto-fetch loop polls with) — there's no per-request correlation id, and the `_mesh_request_lock` defined in `base.py` is never actually acquired anywhere in this version of the library (2.3.7). Since `start_auto_message_fetching()`'s background loop is continuously calling `get_msg()` the whole time the bot runs, an `ERROR`/`MSG_SENT` meant for one in-flight command can occasionally resolve a *different* concurrent command's waiting future instead — this is what caused auto-replies (fired concurrently with that background poll) to intermittently silently fail while manually-typed REPL sends (issued at human-scale intervals, rarely overlapping the poll) worked reliably. Worked around on our side, not in the library, via `send_private`/`send_channel`: a `command_lock` (`asyncio.Lock`) serializes all of *our own* outgoing commands to remove self-inflicted collisions, and `SEND_ATTEMPTS` retries in `send_private` absorb the residual races against the library's own internal polling that we can't otherwise synchronize with. If a future `meshcore` release actually wires up `_mesh_request_lock`, this workaround becomes redundant but harmless.
- **Interactive prompt / logging interaction**: the persistent bottom input is `prompt_toolkit`'s `PromptSession`, wrapped in `patch_stdout()` so log lines print above the input instead of corrupting it. `patch_stdout()` replaces *both* `sys.stdout` and `sys.stderr` with a proxy, but `logging.StreamHandler` captures whichever stream object exists *at handler-construction time* — so `logging.basicConfig(..., force=True)` is called *inside* the `with patch_stdout():` block (not at module import time) to bind the handler to the proxy. If a future change moves logging setup back to import time, the prompt line will get visually clobbered by log output.
