"""Connects to a MeshCore companion node over BLE, logs all private
messages and channel messages it sees, replies 'pong' to any private
message containing 'ping', and offers an interactive prompt for
sending messages and commands.
"""
import asyncio
import json
import logging

from meshcore import EventType, MeshCore
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

CONFIG_PATH = "config.json"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
CHANNEL_PROBE_COUNT = 8
DEFAULT_AUTO_PONG_INTERVAL = 30
AUTO_PONG_COUNT = 100
SEND_ATTEMPTS = 3
SEND_RETRY_DELAY = 0.5

HELP_TEXT = """Commands:
  @Name message        Send a private message to a contact (e.g. @Scoot-Wio hey)
  @#channel message     Send a message to a channel (e.g. @#test hey all, @Public hi)
  message               Send to the last-used target, no @ needed
  /advert [flood]        Broadcast a self-advertisement
  /help                  Show this help
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


async def send_private(meshcore, lock, dst, label, text, hops=None):
    # meshcore's send() matches responses by event type only (no per-request
    # correlation id), and the library's own background auto-fetch loop is
    # concurrently issuing get_msg() calls the whole time this bot runs. The
    # two can race: a MSG_SENT/ERROR meant for one call occasionally resolves
    # the other's waiting future instead, which looks like a send that just
    # silently fails or times out waiting for an ACK. `lock` serializes our
    # own outgoing commands to remove self-inflicted collisions, and the
    # retry loop below absorbs the residual races against the library's
    # internal polling that we can't otherwise synchronize with.
    suffix = f" ({hops})" if hops else ""
    for attempt in range(1, SEND_ATTEMPTS + 1):
        async with lock:
            result = await meshcore.commands.send_msg(dst, text)

        if not result.is_error():
            expected_ack = result.payload["expected_ack"].hex()
            timeout = result.payload["suggested_timeout"] / 1000
            ack = await meshcore.wait_for_event(
                EventType.ACK, attribute_filters={"code": expected_ack}, timeout=timeout
            )
            if ack is not None:
                # The ACK packet itself carries no hop count, so callers
                # that pass `hops` are reusing the hop count of the message
                # being replied to as an estimate of the return path length.
                logger.info("[SENT] (%s) %s -- ACKed%s", label, text, suffix)
                return

        if attempt < SEND_ATTEMPTS:
            await asyncio.sleep(SEND_RETRY_DELAY)

    logger.warning(
        "[SENT] (%s) %s -- no ACK after %d attempt(s), may not have arrived%s",
        label, text, SEND_ATTEMPTS, suffix,
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
        # (Re)configured here, after patch_stdout is active, so log output
        # is routed through the same proxy that keeps the prompt line at
        # the bottom of the screen instead of being overwritten by it.
        logging.basicConfig(
            level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT, force=True
        )

        address = config.get("address") or None
        pin = config.get("pin") or None

        if address is None:
            logger.info("No address configured, scanning for a MeshCore BLE device...")

        meshcore = await MeshCore.create_ble(address=address, pin=pin)
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

        async def handle_private_message(event):
            text = event.payload.get("text", "")
            pubkey_prefix = event.payload.get("pubkey_prefix")
            hops = format_hops(event.payload.get("path_len"))

            contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
            sender = contact.get("adv_name") if contact else pubkey_prefix
            logger.info("[RECV] (%s): %s (%s)", sender, text, hops)

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
            elif "ping" in text.lower():
                await send_private(meshcore, command_lock, pubkey_prefix, sender, "pong", hops=hops)

        async def handle_channel_message(event):
            text = event.payload.get("text", "")
            channel_idx = event.payload.get("channel_idx")
            hops = format_hops(event.payload.get("path_len"))

            channel_name = channel_by_idx.get(channel_idx, f"channel {channel_idx}")
            logger.info("[RECV] (#%s): %s (%s)", channel_name, text, hops)

        async def handle_input_line(line):
            nonlocal current_target

            line = line.strip()
            if not line:
                return

            if line.startswith("/"):
                cmd, _, rest = line[1:].partition(" ")
                cmd = cmd.lower()
                if cmd == "help":
                    print(HELP_TEXT)
                elif cmd == "advert":
                    flood = rest.strip().lower() == "flood"
                    async with command_lock:
                        result = await meshcore.commands.send_advert(flood=flood)
                    if result.is_error():
                        logger.error("/advert failed: %s", result.payload)
                    else:
                        logger.info("Advert sent%s", " (flood)" if flood else "")
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
            await meshcore.stop_auto_message_fetching()
            await meshcore.disconnect()
            logger.info("Disconnected, bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
