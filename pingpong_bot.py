"""Connects to a MeshCore companion node over BLE, logs all private
messages and channel messages it sees, and replies 'pong' to any
private message containing 'ping'.
"""
import asyncio
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from meshcore import EventType, MeshCore  # noqa: E402  (logging must be configured first)

logger = logging.getLogger("pingpong_bot")

CONFIG_PATH = "config.json"


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def format_hops(path_len):
    if path_len == 255:
        return "direct"
    return f"{path_len} hop" + ("" if path_len == 1 else "s")


async def main():
    config = load_config()

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

    channel_names = {}

    async def get_channel_name(channel_idx):
        if channel_idx not in channel_names:
            result = await meshcore.commands.get_channel(channel_idx)
            if result.is_error():
                channel_names[channel_idx] = f"channel {channel_idx}"
            else:
                channel_names[channel_idx] = result.payload["channel_name"] or f"channel {channel_idx}"
        return channel_names[channel_idx]

    async def handle_private_message(event):
        text = event.payload.get("text", "")
        pubkey_prefix = event.payload.get("pubkey_prefix")
        hops = format_hops(event.payload.get("path_len"))

        contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
        sender = contact.get("adv_name") if contact else pubkey_prefix
        logger.info("[RECV] (%s): %s (%s)", sender, text, hops)

        if "ping" not in text.lower():
            return

        result = await meshcore.commands.send_msg(pubkey_prefix, "pong")
        if result.is_error():
            logger.error("[SENT] (%s) pong -- failed to queue: %s", sender, result.payload)
            return

        expected_ack = result.payload["expected_ack"].hex()
        timeout = result.payload["suggested_timeout"] / 1000
        ack = await meshcore.wait_for_event(
            EventType.ACK, attribute_filters={"code": expected_ack}, timeout=timeout
        )
        if ack is None:
            logger.warning("[SENT] (%s) pong -- no ACK, may not have arrived", sender)
        else:
            # The ACK packet itself carries no hop count, so this reuses the
            # incoming ping's hop count as the best available estimate of
            # the return path length.
            logger.info("[SENT] (%s) pong -- ACKed (%s)", sender, hops)

    async def handle_channel_message(event):
        text = event.payload.get("text", "")
        channel_idx = event.payload.get("channel_idx")
        hops = format_hops(event.payload.get("path_len"))

        channel_name = await get_channel_name(channel_idx)
        logger.info("[RECV] (#%s): %s (%s)", channel_name, text, hops)

    dm_subscription = meshcore.subscribe(EventType.CONTACT_MSG_RECV, handle_private_message)
    channel_subscription = meshcore.subscribe(EventType.CHANNEL_MSG_RECV, handle_channel_message)
    await meshcore.start_auto_message_fetching()

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
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
