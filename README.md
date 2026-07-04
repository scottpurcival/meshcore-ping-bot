# MeshcoreChatterbot

A small bot that connects to a MeshCore companion radio node over BLE,
logs private and channel messages it sees, and replies `pong` to any
private message containing `ping`. It also gives you a live prompt for
sending messages and commands yourself.

## Setup

1. Create a virtual environment and install dependencies:

   Windows:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

   Linux:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

   On Linux, BLE goes through BlueZ, so make sure `bluez` is installed and
   `bluetoothd` is running (e.g. `sudo apt install bluez` on Debian/Ubuntu).
   If BLE scanning fails, `bluetoothctl` is the first place to check that
   the adapter is up and not blocked.

2. Copy `config.example.json` to `config.json` (the latter is gitignored
   since it holds your specific device's address).

3. Make sure your MeshCore companion device is powered on and not already
   connected to another app (e.g. the MeshCore phone app) — BLE devices
   usually only accept one connection at a time.

4. Leave `address` in `config.json` as `null` to have the bot auto-scan for
   and connect to the first device advertising as `MeshCore...`. If you have
   more than one MeshCore device nearby, or scanning doesn't find yours, run:

   ```
   .venv\Scripts\python.exe scan_ble.py     # Windows
   .venv/bin/python scan_ble.py             # Linux
   ```

   to list nearby BLE devices with their name and address, then set
   `address` in `config.json` to the MAC address of the right one (e.g.
   `"AA:BB:CC:DD:EE:FF"`).

5. If your device requires a BLE pairing PIN, set it in `config.json`'s
   `pin` field; otherwise leave it `null`.

## Running

Windows: double-click `run_bot.bat`, or run manually:

```
.venv\Scripts\python.exe pingpong_bot.py
```

Linux: run `./run_bot.sh`, or manually:

```
.venv/bin/python pingpong_bot.py
```

The bot is a persistent foreground process with an interactive prompt, so
on a headless Linux server run it inside `tmux` or `screen` — that keeps
the session (and the `>` prompt) alive across an SSH disconnect.

The console will log connection status and every message it sees or
sends. A `>` prompt at the bottom stays put while log output scrolls
above it, and accepts:

- `@Name message` — send a private message to a contact, e.g. `@Scoot-Wio hey`
- `@#channel message` — send to a channel, e.g. `@#test hey all` or `@Public hi`
- `message` (no `@`) — send to whichever target you last used
- `/advert` or `/advert flood` — broadcast a self-advertisement
- `/login <target> <password>` — log in to a remote node's admin CLI (e.g. a repeater)
- `/cmd <target> <text>` — send an admin CLI command; the reply shows up as a normal `[RECV]` line
- `/verbose` — toggle `[HEARD]` logging of every packet the radio hears (off by default since it can get noisy)
- `/help` — list commands

Ctrl+C or Ctrl+D to stop.

Every run also writes the same log output to a timestamped file under
`logs/` (e.g. `logs/pingpong_20260704_142447.log`), gitignored, so you
have a permanent record to review afterward.
