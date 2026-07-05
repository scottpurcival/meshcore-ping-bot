"""Connects to a MeshCore companion node over BLE, logs all private
messages and channel messages it sees, replies 'pong' to any private
message containing 'ping', and offers an interactive prompt for
sending messages and commands.
"""
import asyncio
import json
import logging
import os
from datetime import datetime

from meshcore import EventType, MeshCore
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

CONFIG_PATH = "config.json"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_DIR = "logs"
CHANNEL_PROBE_COUNT = 8
DEFAULT_AUTO_PONG_INTERVAL = 30
AUTO_PONG_COUNT = 100
REPLY_DELAY = 1.0
PONG_CHANNEL_NAME = "dalby"
SEND_ATTEMPTS = 3
SEND_RETRY_DELAY = 0.5
ACK_TIMEOUT_MULTIPLIER = 3

HELP_TEXT = """Commands:
  @Name message           Send a private message to a contact (e.g. @Scoot-Wio hey)
  @#channel message        Send a message to a channel (e.g. @#test hey all, @Public hi)
  message                  Send to the last-used target, no @ needed
  /advert [flood]           Broadcast a self-advertisement
  /login <target> <pwd>     Log in to a remote node's admin CLI (e.g. a repeater)
  /cmd <target> <text>      Send an admin CLI command; reply shows as [RECV]
  /verbose                  Toggle on-screen [HEARD] logging of every packet heard (off by
                            default; always recorded to the log file regardless)
  /help                     Show this help
"""

DM_HELP_TEXT = (
    "Commands: ping - reply pong | help - this list | "
    "start <s> - auto-pong every <s>s (default 30) up to 100 | "
    "stop - stop auto-pong"
)

logger = logging.getLogger("pingpong_bot")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def format_hops(path_len):
    if path_len == 255:
        return "direct"
    return f"{path_len} hop" + ("" if path_len == 1 else "s")


def format_snr(snr):
    return f", SNR {snr:.1f}dB" if snr is not None else ""


def format_path(path_len, path):
    if path_len is None:
        return "unknown path"
    if path_len == -1:
        return "flood"
    if path_len == 0:
        return "direct"
    relays = ",".join(path[i:i + 2] for i in range(0, len(path), 2)[:path_len])
    return f"{path_len} hop" + ("" if path_len == 1 else "s") + f" via {relays}"


def resolve_send_path(meshcore, dst):
    # The contact's own out_path is the real outgoing route, refetched fresh
    # on every attempt since it can change between retries (e.g. after a
    # path reset) -- unlike the incoming message's path_len, this isn't a
    # reused guess.
    prefix = dst.get("public_key", "")[:12] if isinstance(dst, dict) else dst
    contact = meshcore.get_contact_by_key_prefix(prefix)
    if contact is None:
        return "unknown path"
    return format_path(contact.get("out_path_len"), contact.get("out_path", ""))


async def send_private(meshcore, lock, dst, label, text):
    # `lock` serializes our own outgoing commands, since meshcore's send()
    # matches responses by event type only (no per-request correlation id)
    # and could in principle cross-wire two of our own concurrent requests.
    # The bigger factor in practice: contacts routed via a relay (out_path_len
    # > 0) can take longer for a delivery ACK to round-trip than the device's
    # own `suggested_timeout` accounts for, so we wait a multiple of it below
    # rather than trusting it as a hard cutoff -- see the "no ACK" debugging
    # session in git history for how this was diagnosed.
    for attempt in range(1, SEND_ATTEMPTS + 1):
        # Encode the retry number into the actual transmitted text (not just
        # our own log) so the recipient can tell which attempt got through.
        send_text = text if attempt == 1 else f"{text} (retry {attempt - 1})"
        path = resolve_send_path(meshcore, dst)

        async with lock:
            result = await meshcore.commands.send_msg(dst, send_text)

        if result.is_error():
            logger.error("[SENT] (%s) %s -- failed to queue: %s", label, send_text, result.payload)
        else:
            logger.info("[SENT] (%s) %s (%s)", label, send_text, path)
            expected_ack = result.payload["expected_ack"].hex()
            ack_timeout = result.payload["suggested_timeout"] / 1000 * ACK_TIMEOUT_MULTIPLIER
            ack = await meshcore.wait_for_event(
                EventType.ACK, attribute_filters={"code": expected_ack}, timeout=ack_timeout
            )
            if ack is not None:
                logger.info("[ACK] (%s) %s -- delivered (%s)", label, send_text, path)
                return
            logger.warning(
                "[ACK] (%s) %s -- no ACK after %.0fs (%s)", label, send_text, ack_timeout, path
            )

        if attempt < SEND_ATTEMPTS:
            logger.info(
                "[SENT] (%s) %s -- retrying (attempt %d/%d)", label, send_text, attempt + 1, SEND_ATTEMPTS
            )
            await asyncio.sleep(SEND_RETRY_DELAY)

    logger.warning(
        "[SENT] (%s) %s -- giving up after %d attempt(s) (%s)", label, send_text, SEND_ATTEMPTS, path
    )


async def run_auto_pong(meshcore, lock, pubkey_prefix, sender, interval):
    for i in range(1, AUTO_PONG_COUNT + 1):
        await send_private(meshcore, lock, pubkey_prefix, sender, f"pong {i:03d}")
        if i < AUTO_PONG_COUNT:
            await asyncio.sleep(interval)


async def send_channel(meshcore, lock, channel_idx, label, text):
    async with lock:
        result = await meshcore.commands.send_chan_msg(channel_idx, text)
    if result.is_error():
        logger.error("[SENT] (#%s) %s -- failed: %s", label, text, result.payload)
    else:
        logger.info("[SENT] (#%s) %s", label, text)


def resolve_target(meshcore, channel_by_name, raw_target):
    """Resolve an '@target' string to a ('contact', contact_dict, label) or
    ('channel', channel_idx, label) tuple, or None if nothing matches."""
    if raw_target.startswith("#"):
        name = raw_target[1:]
        idx = channel_by_name.get(name.lower())
        return ("channel", idx, name) if idx is not None else None

    contact = meshcore.get_contact_by_name(raw_target)
    if contact is not None:
        return ("contact", contact, contact.get("adv_name", raw_target))

    idx = channel_by_name.get(raw_target.lower())
    if idx is not None:
        return ("channel", idx, raw_target)

    return None


async def main():
    config = load_config()

    with patch_stdout():
        # (Re)configured here, after patch_stdout is active, so console log
        # output is routed through the same proxy that keeps the prompt line
        # at the bottom of the screen instead of being overwritten by it.
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"pingpong_{datetime.now():%Y%m%d_%H%M%S}.log")
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        logging.basicConfig(
            level=logging.INFO,
            format=LOG_FORMAT,
            datefmt=LOG_DATEFMT,
            force=True,
            handlers=[stream_handler, file_handler],
        )
        # Only our own logger goes to DEBUG -- third-party loggers (bleak's BLE
        # stack in particular) have no explicit level of their own, so they'd
        # inherit the root's level too and flood the log with GATT/notify
        # chatter on every (re)connect if root itself were bumped to DEBUG.
        logger.setLevel(logging.DEBUG)
        logger.info("Logging to %s", log_path)

        address = config.get("address") or None
        pin = config.get("pin") or None

        if address is None:
            logger.info("No address configured, scanning for a MeshCore BLE device...")

        meshcore = await MeshCore.create_ble(
            address=address, pin=pin, auto_reconnect=True, max_reconnect_attempts=float("inf")
        )
        if meshcore is None:
            logger.error("Could not connect to companion node over BLE")
            return

        logger.info("Connected to companion node over BLE (%s)", meshcore.cx.connection.address)

        await meshcore.ensure_contacts()

        command_lock = asyncio.Lock()

        channel_by_idx = {}
        channel_by_name = {}
        for idx in range(CHANNEL_PROBE_COUNT):
            async with command_lock:
                result = await meshcore.commands.get_channel(idx)
            if result.is_error():
                continue
            name = result.payload.get("channel_name")
            if name:
                channel_by_idx[idx] = name
                channel_by_name[name.lower()] = idx

        current_target = None
        auto_pong_tasks = {}
        verbose = False

        async def handle_private_message(event):
            text = event.payload.get("text", "")
            pubkey_prefix = event.payload.get("pubkey_prefix")
            hops = format_hops(event.payload.get("path_len"))
            snr = format_snr(event.payload.get("SNR"))

            contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
            sender = contact.get("adv_name") if contact else pubkey_prefix
            logger.info("[RECV] (%s): %s (%s%s)", sender, text, hops, snr)

            # Receiving a private message that requested delivery confirmation
            # makes the companion's own firmware queue an outgoing ack for it.
            # Replying instantly asks the radio to also queue our own new
            # message at almost the same moment -- this gives the firmware's
            # ack a head start so the two don't contend for the same TX slot.
            await asyncio.sleep(REPLY_DELAY)

            parts = text.strip().split()
            command = parts[0].lower() if parts else ""

            if command == "help":
                await send_private(meshcore, command_lock, pubkey_prefix, sender, DM_HELP_TEXT)
            elif command == "start":
                interval = DEFAULT_AUTO_PONG_INTERVAL
                if len(parts) > 1:
                    try:
                        interval = max(1, int(parts[1]))
                    except ValueError:
                        pass

                existing = auto_pong_tasks.pop(pubkey_prefix, None)
                if existing:
                    existing.cancel()

                task = asyncio.create_task(
                    run_auto_pong(meshcore, command_lock, pubkey_prefix, sender, interval)
                )

                def clear_if_current(finished_task, key=pubkey_prefix):
                    if auto_pong_tasks.get(key) is finished_task:
                        auto_pong_tasks.pop(key, None)

                task.add_done_callback(clear_if_current)
                auto_pong_tasks[pubkey_prefix] = task
            elif command == "stop":
                task = auto_pong_tasks.pop(pubkey_prefix, None)
                if task:
                    task.cancel()
            elif text.lower().startswith("ping"):
                await send_private(meshcore, command_lock, pubkey_prefix, sender, "pong")

        async def handle_channel_message(event):
            text = event.payload.get("text", "")
            channel_idx = event.payload.get("channel_idx")
            hops = format_hops(event.payload.get("path_len"))
            snr = format_snr(event.payload.get("SNR"))

            channel_name = channel_by_idx.get(channel_idx, f"channel {channel_idx}")
            logger.info("[RECV] (#%s): %s (%s%s)", channel_name, text, hops, snr)

            # The firmware has no separate sender-identity field for channel
            # messages -- it prepends "SenderName: " to the text itself, so
            # the ping check has to look past that prefix rather than at the
            # start of the raw text (unlike private messages, which have no
            # such prefix).
            _, sep, message = text.partition(": ")
            message = message if sep else text

            if channel_name.lstrip("#").lower() == PONG_CHANNEL_NAME and message.strip().lower().startswith("ping"):
                await asyncio.sleep(REPLY_DELAY)
                await send_channel(meshcore, command_lock, channel_idx, channel_name, "pong")

        async def handle_rx_log(event):
            # Fires for every packet the radio hears at all, delivered to us
            # or not -- including a relay's rebroadcast of someone else's
            # packet. Matching pkt_hash across two [HEARD] lines (one direct,
            # one via a relay's path) confirms that relay actually forwarded
            # it, which [RECV] alone can't show. Always logged at DEBUG so
            # the file always has it; noisy on a busy mesh, so it's only
            # shown on screen when /verbose raises the console handler's
            # level to DEBUG too.
            path = format_path(event.payload.get("path_len"), event.payload.get("path", ""))
            snr = format_snr(event.payload.get("snr"))
            rssi = event.payload.get("rssi")
            rssi_str = f", RSSI {rssi}dBm" if rssi is not None else ""
            payload_type = event.payload.get("payload_typename", "?")
            pkt_hash = event.payload.get("pkt_hash")
            pkt_str = f" pkt={pkt_hash:08x}" if pkt_hash is not None else ""

            logger.debug("[HEARD]%s %s (%s%s%s)", pkt_str, payload_type, path, snr, rssi_str)

        async def handle_connected(event):
            if event.payload.get("reconnected"):
                logger.info("Reconnected to companion node over BLE")
            else:
                logger.info("Connected to companion node over BLE")

        async def handle_disconnected(event):
            reason = event.payload.get("reason", "unknown")
            if reason == "manual_disconnect":
                logger.info("Disconnected from companion node (shutting down)")
            elif event.payload.get("max_attempts_exceeded"):
                logger.error(
                    "Lost connection to companion node (%s) -- reconnect attempts exhausted, giving up",
                    reason,
                )
            else:
                logger.warning("Lost connection to companion node (%s) -- reconnecting...", reason)

        async def handle_input_line(line):
            nonlocal current_target, verbose

            line = line.strip()
            if not line:
                return

            if line.startswith("/"):
                cmd, _, rest = line[1:].partition(" ")
                cmd = cmd.lower()
                if cmd == "help":
                    print(HELP_TEXT)
                elif cmd == "verbose":
                    verbose = not verbose
                    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
                    logger.info(
                        "On-screen [HEARD] logging %s (log file always has it)",
                        "enabled" if verbose else "disabled",
                    )
                elif cmd == "advert":
                    flood = rest.strip().lower() == "flood"
                    async with command_lock:
                        result = await meshcore.commands.send_advert(flood=flood)
                    if result.is_error():
                        logger.error("/advert failed: %s", result.payload)
                    else:
                        logger.info("Advert sent%s", " (flood)" if flood else "")
                elif cmd == "login":
                    target_str, _, password = rest.partition(" ")
                    resolved = resolve_target(meshcore, channel_by_name, target_str)
                    if resolved is None or resolved[0] != "contact":
                        print(f"Unknown contact: {target_str}")
                        return
                    contact = resolved[1]
                    async with command_lock:
                        result = await meshcore.commands.send_login_sync(contact, password)
                    if result is None:
                        logger.warning("Login to %s failed or timed out", resolved[2])
                    else:
                        logger.info("Logged in to %s", resolved[2])
                elif cmd == "cmd":
                    target_str, _, admin_cmd = rest.partition(" ")
                    if not admin_cmd:
                        print("Usage: /cmd <target> <admin command text>")
                        return
                    resolved = resolve_target(meshcore, channel_by_name, target_str)
                    if resolved is None or resolved[0] != "contact":
                        print(f"Unknown contact: {target_str}")
                        return
                    contact = resolved[1]
                    async with command_lock:
                        result = await meshcore.commands.send_cmd(contact, admin_cmd)
                    if result.is_error():
                        logger.error("/cmd to %s failed: %s", resolved[2], result.payload)
                    else:
                        logger.info("Sent admin command to %s -- reply will appear as [RECV]", resolved[2])
                else:
                    print(f"Unknown command: /{cmd}. Type /help for a list.")
                return

            if line.startswith("@"):
                target_str, _, text = line[1:].partition(" ")
                text = text.strip()
                if not text:
                    print("No message text provided.")
                    return
                resolved = resolve_target(meshcore, channel_by_name, target_str)
                if resolved is None:
                    print(f"Unknown target: {target_str}")
                    return
                current_target = resolved
            else:
                text = line
                if current_target is None:
                    print("No target set yet -- start a message with @Name or @#channel.")
                    return
                resolved = current_target

            kind, ident, label = resolved
            if kind == "contact":
                await send_private(meshcore, command_lock, ident, label, text)
            else:
                await send_channel(meshcore, command_lock, ident, label, text)

        dm_subscription = meshcore.subscribe(EventType.CONTACT_MSG_RECV, handle_private_message)
        channel_subscription = meshcore.subscribe(EventType.CHANNEL_MSG_RECV, handle_channel_message)
        rxlog_subscription = meshcore.subscribe(EventType.RX_LOG_DATA, handle_rx_log)
        connected_subscription = meshcore.subscribe(EventType.CONNECTED, handle_connected)
        disconnected_subscription = meshcore.subscribe(EventType.DISCONNECTED, handle_disconnected)
        await meshcore.start_auto_message_fetching()

        logger.info("Bot is running. Type /help for commands, Ctrl+C to stop.")
        session = PromptSession()
        try:
            while True:
                try:
                    line = await session.prompt_async("> ")
                except (EOFError, KeyboardInterrupt):
                    break
                await handle_input_line(line)
        finally:
            for task in auto_pong_tasks.values():
                task.cancel()
            meshcore.unsubscribe(dm_subscription)
            meshcore.unsubscribe(channel_subscription)
            meshcore.unsubscribe(rxlog_subscription)
            meshcore.unsubscribe(connected_subscription)
            meshcore.unsubscribe(disconnected_subscription)
            await meshcore.stop_auto_message_fetching()
            await meshcore.disconnect()
            logger.info("Disconnected, bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
