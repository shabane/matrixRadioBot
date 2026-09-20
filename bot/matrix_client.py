"""
Async Matrix Client for Radio Bot
Handles long-polling sync, auto-accepts invites, and dispatches messages.
"""

import asyncio
import logging
import uuid
import re
from typing import Optional, Callable, Dict, Any
import aiohttp

logger = logging.getLogger("radio.matrix")


def markdown_to_html(text: str) -> str:
    """Simple markdown to HTML formatter for Matrix formatted_body."""
    html = text
    # Bold
    html = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', html)
    # Code inline
    html = re.sub(r'`(.*?)`', r'<code>\1</code>', html)
    # Blockquote
    lines = html.splitlines()
    formatted_lines = []
    in_quote = False
    for line in lines:
        if line.startswith("> "):
            if not in_quote:
                formatted_lines.append("<blockquote>")
                in_quote = True
            formatted_lines.append(line[2:])
        else:
            if in_quote:
                formatted_lines.append("</blockquote>")
                in_quote = False
            formatted_lines.append(line)
    if in_quote:
        formatted_lines.append("</blockquote>")
    html = "<br/>".join(formatted_lines)
    return html


class MatrixClient:
    def __init__(self, homeserver_url: str, user_id: str, access_token: str, auto_join_invites: bool = True):
        self.homeserver_url = homeserver_url.rstrip("/")
        self.user_id = user_id
        self.access_token = access_token
        self.auto_join_invites = auto_join_invites

        self.session: Optional[aiohttp.ClientSession] = None
        self.next_batch: Optional[str] = None
        self._running = False
        self._message_handler: Optional[Callable] = None
        self._joined_rooms: set[str] = set()

    def set_message_handler(self, handler: Callable):
        self._message_handler = handler

    async def start(self):
        """Starts the Matrix client session and sync loop."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        self.session = aiohttp.ClientSession(headers=headers)
        self._running = True

        logger.info("Initializing Matrix sync for user: %s", self.user_id)

        # Initial sync to skip old messages before bot start
        try:
            url = f"{self.homeserver_url}/_matrix/client/v3/sync?filter={{\"room\":{{\"timeline\":{{\"limit\":1}}}}}}"
            async with self.session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.next_batch = data.get("next_batch")
                    # populate initially joined rooms
                    joined = data.get("rooms", {}).get("join", {})
                    self._joined_rooms.update(joined.keys())
                    logger.info("Initial sync done. next_batch: %s, joined rooms: %d", self.next_batch, len(self._joined_rooms))
                else:
                    text = await resp.text()
                    logger.warning("Initial sync returned status %d: %s", resp.status, text)
        except Exception as e:
            logger.warning("Error during initial sync: %s", e)

        # Main sync loop
        while self._running:
            try:
                await self._sync_step()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Sync loop error: %s. Retrying in 5s...", e)
                await asyncio.sleep(5)

    async def stop(self):
        """Stops the sync loop and closes session."""
        self._running = False
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("Matrix client stopped.")

    async def _sync_step(self):
        """Performs a single long-polling sync request."""
        assert self.session is not None
        params = {"timeout": "30000"}
        if self.next_batch:
            params["since"] = self.next_batch

        url = f"{self.homeserver_url}/_matrix/client/v3/sync"
        async with self.session.get(url, params=params, timeout=45) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning("Sync failed (HTTP %d): %s", resp.status, text)
                await asyncio.sleep(3)
                return

            data = await resp.json()
            self.next_batch = data.get("next_batch", self.next_batch)

            rooms = data.get("rooms", {})

            # 1. Handle Invites
            invites = rooms.get("invite", {})
            for room_id, invite_data in invites.items():
                if self.auto_join_invites:
                    asyncio.create_task(self._auto_join_room(room_id))

            # 2. Handle Joined Rooms
            joined = rooms.get("join", {})
            for room_id, room_data in joined.items():
                self._joined_rooms.add(room_id)
                timeline = room_data.get("timeline", {})
                events = timeline.get("events", [])
                for ev in events:
                    await self._process_event(room_id, ev)

    async def _auto_join_room(self, room_id: str):
        """Automatically joins an invited room and sends a greeting."""
        assert self.session is not None
        logger.info("Auto-joining invited room: %s", room_id)
        url = f"{self.homeserver_url}/_matrix/client/v3/join/{room_id}"
        try:
            async with self.session.post(url, json={}) as resp:
                if resp.status == 200:
                    logger.info("Successfully joined room: %s", room_id)
                    self._joined_rooms.add(room_id)
                    await self.send_message(
                        room_id,
                        "📻 **سلام! ربات رادیو با موفقیت اضافه شد.**\nبرای پخش موزیک در کال صوتی کافیست لینک را بفرستید: `@radio <لینک>`\nبرای راهنما: `@radio help`"
                    )
                else:
                    text = await resp.text()
                    logger.error("Failed to join room %s (HTTP %d): %s", room_id, resp.status, text)
        except Exception as e:
            logger.error("Error auto-joining room %s: %s", room_id, e)

    async def _process_event(self, room_id: str, event: Dict[str, Any]):
        """Processes a single timeline event."""
        ev_type = event.get("type")
        if ev_type != "m.room.message":
            return

        content = event.get("content", {})
        msg_type = content.get("msgtype")
        if msg_type != "m.text":
            return

        body = content.get("body", "")
        sender = event.get("sender", "")

        if sender == self.user_id:
            return

        # Simple heuristic for Direct Message (PV): check if room is known direct or single user
        # We can pass is_direct=False and rely on command parser
        is_direct = False

        if self._message_handler:
            try:
                await self._message_handler(room_id, sender, body, is_direct)
            except Exception as e:
                logger.error("[%s] Error handling message: %s", room_id, e)

    async def send_message(self, room_id: str, text: str):
        """Sends a text message with HTML formatting to room_id."""
        if not self.session or self.session.closed:
            return

        txn_id = uuid.uuid4().hex
        url = f"{self.homeserver_url}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"

        formatted_html = markdown_to_html(text)

        payload = {
            "msgtype": "m.text",
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_html
        }

        try:
            async with self.session.put(url, json=payload, timeout=10) as resp:
                if resp.status not in (200, 201):
                    err_text = await resp.text()
                    logger.error("[%s] Failed to send message (HTTP %d): %s", room_id, resp.status, err_text)
        except Exception as e:
            logger.error("[%s] Error sending message: %s", room_id, e)
