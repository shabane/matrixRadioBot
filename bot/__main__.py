"""
Matrix Radio Bot Entrypoint
"""

import os
import asyncio
import logging
import signal
import sys
import time
import yaml
from pathlib import Path
from urllib.parse import urlparse

from bot.matrix_client import MatrixClient
from bot.queue_manager import QueueManager, IDLE_TIMEOUT_SECONDS
from bot.command_handler import CommandHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("radio.main")


def load_config(config_path: str = "config.yaml") -> dict:
    p = Path(config_path)
    if not p.is_file():
        # check parent directory
        p = Path(__file__).resolve().parent.parent / "config.yaml"
    if not p.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main():
    config = load_config()

    matrix_cfg = config.get("matrix", {})
    lk_cfg = config.get("livekit", {})
    proxy_cfg = config.get("proxy", {})
    audio_cfg = config.get("audio", {})

    homeserver_url = matrix_cfg["homeserver_url"]
    user_id = matrix_cfg["user_id"]
    access_token = matrix_cfg["access_token"]
    bot_name = matrix_cfg.get("bot_name", "radio")
    auto_join = matrix_cfg.get("auto_join_invites", True)

    jwt_service_url = lk_cfg.get("jwt_service_url", f"{homeserver_url}/livekit/jwt/sfu/get")
    parsed_hs = urlparse(homeserver_url)
    sfu_url = lk_cfg.get("sfu_url", f"wss://{parsed_hs.netloc}/livekit/sfu")

    # Proxy configuration: environment variable RADIO_PROXY_URL takes highest precedence
    proxy_url = (
        os.environ.get("RADIO_PROXY_URL")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("HTTP_PROXY")
        or proxy_cfg.get("url", "")
    ).strip()

    logger.info("Initializing Matrix Radio Bot...")
    logger.info("Homeserver: %s | User: %s", homeserver_url, user_id)
    if proxy_url:
        logger.info("Proxy configured: %s", proxy_url)
    else:
        logger.info("Direct connection (no proxy configured).")

    matrix_client = MatrixClient(
        homeserver_url=homeserver_url,
        user_id=user_id,
        access_token=access_token,
        auto_join_invites=auto_join
    )

    idle_timeout_sec = audio_cfg.get("idle_leave_timeout_sec", IDLE_TIMEOUT_SECONDS)

    queue_manager = QueueManager(
        homeserver_url=homeserver_url,
        user_id=user_id,
        access_token=access_token,
        jwt_service_url=jwt_service_url,
        sfu_url=sfu_url,
        send_message_callback=matrix_client.send_message,
        proxy_url=proxy_url if proxy_url else None,
        idle_timeout_sec=idle_timeout_sec
    )

    safe_config = {
        "homeserver_url": homeserver_url,
        "bot_name": bot_name,
        "auto_join_invites": auto_join,
        "sfu_url": sfu_url,
        "jwt_service_url": jwt_service_url,
        "proxy_configured": bool(proxy_url),
        "idle_leave_timeout_sec": idle_timeout_sec,
    }

    command_handler = CommandHandler(
        bot_user_id=user_id,
        bot_name=bot_name,
        queue_manager=queue_manager,
        send_message_callback=matrix_client.send_message,
        app_config=safe_config,
        start_time=time.monotonic()
    )

    matrix_client.set_message_handler(command_handler.handle_message)

    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received. Cleaning up...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    client_task = asyncio.create_task(matrix_client.start())

    await stop_event.wait()

    logger.info("Stopping Matrix client and audio queues...")
    await matrix_client.stop()
    await queue_manager.cleanup_all()

    client_task.cancel()
    try:
        await client_task
    except asyncio.CancelledError:
        pass

    logger.info("Matrix Radio Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
