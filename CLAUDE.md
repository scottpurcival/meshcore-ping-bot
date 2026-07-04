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

Setup (Linux):
```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
BLE on Linux goes through BlueZ (`bleak` pulls in `dbus-fast` automatically via its own platform-conditional dependency metadata, in place of the `winrt-*` packages it pulls on Windows) — make sure `bluez`/`bluetoothd` is installed and running.

Run the bot:
```
.venv\Scripts\python.exe pingpong_bot.py   # Windows
.venv/bin/python pingpong_bot.py           # Linux
```
or `run_bot.bat` (Windows, double-click; pauses on exit so the window doesn't vanish on a crash) / `./run_bot.sh` (Linux). Since it's a persistent foreground process with an interactive prompt, run it inside `tmux`/`screen` on a headless Linux server so the session survives an SSH disconnect.

List nearby BLE devices (name, address, signal strength) to find the right `address` for `config.json`:
```
.venv\Scripts\python.exe scan_ble.py   # Windows
.venv/bin/python scan_ble.py           # Linux
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
- **BLE pairing on Windows**: bleak's WinRT backend only supports "Just Works" pairing (see comment in `bleak/backends/winrt/client.py`), which isn't sufficient if the companion device demands authenticated/MITM pairing. If BLE connect fails with `Insufficient Authentication`, the fix is pairing the device through Windows' own Bluetooth settings UI first (which supports passkey/authenticated pairing), then letting this script connect to the already-bonded device. **On Linux**, bleak uses the BlueZ/D-Bus backend instead, which is not known to have this same "Just Works only" limitation — `pair()` may work directly without an OS-level pre-pair step, but this is unverified in this codebase and should be confirmed empirically the first time this runs against real hardware requiring a pairing PIN on Linux.
- **Delivery ACK timing (important, empirically confirmed)**: `send_msg`'s `expected_ack`/`suggested_timeout` come from the companion's `MSG_SENT` response, but for a contact whose known route is via a relay (`out_path_len > 0` in the contact record, not a direct 0-hop link), the real round trip for a delivery ACK can take noticeably longer than that `suggested_timeout` — we confirmed this live with `debug=True` on `create_ble(...)`: the send and its `expected_ack` were captured cleanly with no event mismatch, the reply physically transmitted (confirmed via the companion's TX LED), but our `wait_for_event(EventType.ACK, ...)` gave up right as the real (delayed) ACK was still in flight, so it landed with nobody listening. `send_private` now waits `suggested_timeout * ACK_TIMEOUT_MULTIPLIER` instead of trusting the raw value, and retries (`SEND_ATTEMPTS`) on top of that. Separately, `CommandHandlerBase.send()` in the library matches responses purely by `EventType` with no per-request correlation id (and a `_mesh_request_lock` defined in `base.py` is never actually acquired anywhere in this version, 2.3.7) — a real latent bug, and `command_lock` in `main()` serializes our own outgoing commands as a defensive measure against it, but live captures did not actually show this cross-wiring as the cause of the "pong never arrives" symptom; the ACK timeout was.
- **Interactive prompt / logging interaction**: the persistent bottom input is `prompt_toolkit`'s `PromptSession`, wrapped in `patch_stdout()` so log lines print above the input instead of corrupting it. `patch_stdout()` replaces *both* `sys.stdout` and `sys.stderr` with a proxy, but `logging.StreamHandler` captures whichever stream object exists *at handler-construction time* — so `logging.basicConfig(..., force=True)` is called *inside* the `with patch_stdout():` block (not at module import time) to bind the handler to the proxy. If a future change moves logging setup back to import time, the prompt line will get visually clobbered by log output.
- **`RX_LOG_DATA` / `path_len` semantics (subtle)**: subscribing to `EventType.RX_LOG_DATA` (the `[HEARD]` log line) surfaces every packet the radio decodes, including a relay's rebroadcast of a message this node itself sent — a device can appear to "hear its own TX" this way, but it's actually hearing the relay's echo, not its own transmission (radios can't RX while TX). The `path_len` on these events is the *packet's own recorded remaining-hop count at that specific reception*, not "hops between original sender and me": the same `pkt_hash` heard straight from the origin shows the full planned path (e.g. `1 hop via 8f`), while the *same packet* heard again after a relay has forwarded it shows `direct` (hop already consumed). Comparing `pkt_hash` across `[HEARD]` lines from two bot instances (one per radio) is the way to confirm whether a given relay is actually forwarding traffic.
- **Reply timing vs. the companion's own auto-ack (`REPLY_DELAY`)**: receiving a private message that requested delivery confirmation makes the companion queue its own outgoing ack for it — separate from, but nearly simultaneous with, whatever reply our bot immediately sends. Since a LoRA radio can only transmit one packet at a time, these two outgoing sends can contend for the same TX slot; live logs from both ends showed a pong attempt that neither side heard at all (not "the relay failed to forward," just nothing was heard, anywhere) — consistent with this kind of self-contention rather than the relay/RF path being unreliable (a later retry over the identical relay path was heard cleanly by both sides). `REPLY_DELAY` (currently 0.5s) is applied in `handle_private_message` before any reply logic to give the companion's own ack a head start; this is a plausible, not yet firmware-confirmed, mitigation.
