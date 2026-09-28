"""
Command Handler for Matrix Radio Bot
Parses user commands, URLs (YouTube, SoundCloud, Spotify, Direct), and search queries.
"""

import re
import shutil
import sys
import time
import logging
from typing import Optional
from bot.queue_manager import QueueManager, IDLE_TIMEOUT_SECONDS
from bot.audio_streamer import FRAME_DURATION_MS, SAMPLE_RATE
from bot.audio_settings import (
    DEFAULT_VOLUME_PERCENT, DEFAULT_NORMALIZE, DEFAULT_FADEIN_MS,
    MIN_VOLUME_PERCENT, MAX_VOLUME_PERCENT, MAX_FADEIN_MS
)
from bot.storage import HISTORY_LIMIT

logger = logging.getLogger("radio.cmd")

URL_REGEX = re.compile(r'https?://[^\s<>"]+')

BANG_ALIASES = {
    "help": "help", "h": "help",
    "join": "join", "j": "join",
    "leave": "leave", "lv": "leave",
    "play": "play", "p": "play",
    "queue": "queue", "q": "queue",
    "nowplaying": "nowplaying", "np": "nowplaying",
    "skip": "skip", "s": "skip",
    "stop": "stop", "x": "stop",
    "resume": "resume", "rs": "resume",
    "loop": "loop", "lp": "loop",
    "loopqueue": "loopqueue", "lq": "loopqueue",
    "progress": "progress", "pr": "progress",
    "history": "history", "hist": "history",
    "save": "save", "sv": "save",
    "load": "load", "ld": "load",
    "queues": "queues", "qs": "queues",
    "deletequeue": "deletequeue", "dq": "deletequeue",
    "renamequeue": "renamequeue", "rq": "renamequeue",
    "audio": "audio", "a": "audio",
    "normalize": "normalize", "norm": "normalize",
    "fadein": "fadein", "fi": "fadein",
    "volume": "volume", "v": "volume",
    "status": "status", "st": "status",
    "diag": "diag", "d": "diag",
    "config": "config", "cfg": "config",
    "defaults": "defaults", "df": "defaults",
}

BANG_HELP_TEXT = (
    "**Playback**\n"
    "`!help` (`!h`) - show this help\n"
    "`!join` (`!j`) - join Element Call in this room\n"
    "`!leave` (`!lv`) - leave current Element Call\n"
    "`!play` (`!p`) `<url-or-query>` - add track/playlist (YouTube, Spotify, SoundCloud) and auto-join call if needed\n"
    "`!queue` (`!q`) - show queue with ETA\n"
    "`!nowplaying` (`!np`) - show current track\n"
    "`!skip` (`!s`) - skip current track\n"
    "`!stop` (`!x`) - stop playback where it is (queue/loop kept, stays connected)\n"
    "`!resume` (`!rs`) - resume playback from where it stopped\n"
    "`!loop` (`!lp`) - toggle single-track loop (mutually exclusive with `!loopqueue`; auto-disables if skipped)\n"
    "`!loopqueue` (`!lq`) - toggle whole-queue loop (replays the queue from the top; mutually exclusive with `!loop`)\n"
    "`!progress` (`!pr`) - show an ASCII progress bar for the current track\n"
    "`!history` (`!hist`) - show recent playback history\n\n"
    "**Saved Queues**\n"
    "`!save` (`!sv`) `<name> [--force]` - save current+upcoming queue\n"
    "`!load` (`!ld`) `<name>` - load a saved queue\n"
    "`!queues` (`!qs`) - list saved queues\n"
    "`!deletequeue` (`!dq`) `<name>` - delete a saved queue\n"
    "`!renamequeue` (`!rq`) `<old> <new>` - rename a saved queue\n\n"
    "**Audio & Info**\n"
    "`!audio` (`!a`) - show current audio settings\n"
    "`!normalize` (`!norm`) `on|off` - toggle normalization\n"
    "`!fadein` (`!fi`) `<ms>` - set fade-in (0-5000)\n"
    "`!volume` (`!v`) `<0-200>` - set playback volume percent\n"
    "`!status` (`!st`) - show bot status\n"
    "`!diag` (`!d`) - show diagnostics\n"
    "`!config` (`!cfg`) - show active config\n"
    "`!defaults` (`!df`) - show default config values"
)


class CommandHandler:
    def __init__(self, bot_user_id: str, bot_name: str, queue_manager: QueueManager, send_message_callback,
                 app_config: Optional[dict] = None, start_time: Optional[float] = None):
        self.bot_user_id = bot_user_id
        self.bot_name = bot_name.lower()
        self.queue_manager = queue_manager
        self.send_message = send_message_callback
        self.app_config = app_config or {}
        self.start_time = start_time if start_time is not None else time.monotonic()

    def is_bot_mentioned(self, text: str) -> bool:
        """Checks if the bot was tagged or mentioned in the message."""
        lower = text.lower()
        if self.bot_user_id.lower() in lower:
            return True
        if f"@{self.bot_name}" in lower:
            return True
        if lower.startswith(f"{self.bot_name}:") or lower.startswith(f"{self.bot_name} "):
            return True
        return False

    def clean_command(self, text: str) -> str:
        """Strips the bot mention from the message text."""
        cleaned = re.sub(re.escape(self.bot_user_id), "", text, flags=re.IGNORECASE)
        cleaned = re.sub(rf"@{self.bot_name}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"^{self.bot_name}[:,\s]+", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    async def handle_message(self, room_id: str, sender: str, text: str, is_direct: bool):
        """Dispatches an incoming room message."""
        if sender == self.bot_user_id:
            return

        text = text.strip()
        if not text:
            return

        # Bang-prefixed commands work everywhere, no mention required.
        if text.startswith("!") and not text.startswith("!!"):
            await self._handle_bang_command(room_id, sender, text[1:])
            return

        # In group rooms, require mention. In PV/Direct chat, always respond.
        if not is_direct and not self.is_bot_mentioned(text):
            return

        cmd = self.clean_command(text)
        cmd_lower = cmd.lower()
        logger.info("[%s] Command from %s: '%s'", room_id, sender, cmd)

        player = await self.queue_manager.get_player(room_id)

        # 1. Pause / Stop (halts in place, keeps queue/loop, stays connected)
        if cmd_lower in ("pause", "stop"):
            paused = await player.pause()
            if paused:
                await self.send_message(room_id, "⏸️ **Playback stopped.** (Use `@radio resume` to continue)")
            else:
                await self.send_message(room_id, "⚠️ Nothing is playing right now.")
            return

        # 2. Resume
        if cmd_lower in ("resume",):
            resumed = await player.resume()
            if resumed:
                await self.send_message(room_id, "▶️ **Playback resumed.**")
            else:
                await self.send_message(room_id, "⚠️ Nothing is paused.")
            return

        # 3. Skip / Next
        if cmd_lower in ("skip", "next"):
            skipped = await player.skip()
            if skipped:
                await self.send_message(room_id, "⏭️ **Skipped the current track.** Moving to the next one...")
            else:
                await self.send_message(room_id, "⚠️ Nothing is playing right now.")
            return

        # 4. Leave
        if cmd_lower in ("leave",):
            await player.leave()
            await self.send_message(room_id, "👋 **Queue cleared and left the call.**")
            return

        # 5. Queue / List
        if cmd_lower in ("queue", "list"):
            queue = player.get_queue()
            curr = player.current_song
            if not curr and not queue:
                await self.send_message(room_id, "📭 **The queue for this chat is empty.**")
                return

            msg_lines = ["📋 **Playback Queue:**"]
            if curr:
                status = "⏸️ [paused]" if player.is_paused else "▶️ [playing]"
                msg_lines.append(f"**Now:** {status} `[{curr.title}]({curr.webpage_url})` (duration: `{curr.duration_str}`)")
            if queue:
                msg_lines.append("\n**Up next:**")
                for i, s in enumerate(queue, 1):
                    msg_lines.append(f"{i}. `[{s.title}]({s.webpage_url})` (duration: `{s.duration_str}`) | requested by: `{s.requested_by}`")
            await self.send_message(room_id, "\n".join(msg_lines))
            return

        # 6. Now Playing (np)
        if cmd_lower in ("np", "now"):
            curr = player.current_song
            if curr:
                status = "⏸️ paused" if player.is_paused else "▶️ playing"
                await self.send_message(
                    room_id,
                    f"🎵 **Now Playing:**\n"
                    f"> **[{curr.title}]({curr.webpage_url})**\n"
                    f"⏱️ Duration: `{curr.duration_str}` | Uploader: `{curr.uploader}`\n"
                    f"Status: {status}\n👤 Requested by: `{curr.requested_by}`"
                )
            else:
                await self.send_message(room_id, "💤 Nothing is playing right now.")
            return

        # 7. Help
        if cmd_lower in ("help", "?"):
            help_text = (
                "📻 **Radio & Music Bot Help (supports YouTube, Spotify, SoundCloud):**\n\n"
                "• `@radio <link>` : play from YouTube, SoundCloud, Spotify, or a direct link\n"
                "• `@radio play <song name>` : search and play a song from YouTube\n"
                "• `@radio pause` : pause playback\n"
                "• `@radio resume` : resume playback\n"
                "• `@radio skip` : skip the current track\n"
                "• `@radio queue` : view the track queue\n"
                "• `@radio np` : view info about the current track\n"
                "• `@radio leave` : leave the call and clear the queue\n\n"
                "💡 *Tip: in a direct message (PV), you don't need to mention `@radio`.*"
            )
            await self.send_message(room_id, help_text)
            return

        # 8. Song Playback (URL or Search Query)
        query = cmd
        # Remove "play"/"search" prefix if present
        for prefix in ("play ", "search "):
            if query.lower().startswith(prefix):
                query = query[len(prefix):].strip()
                break

        if query:
            if player.resolver.is_playlist_url(query):
                await self.send_message(room_id, f"📃 Playlist detected, reading tracks: `{query}`...")
                count = await player.add_playlist(query, requested_by=sender)
                if count:
                    await self.send_message(room_id, f"➕ Found {count} track(s) in the playlist. Adding them to the queue...")
                else:
                    await self.send_message(room_id, "❌ Could not read that playlist.")
                return

            await self.send_message(room_id, f"🔍 Searching for track info: `{query}`...")
            song = await player.add_query(query, requested_by=sender)
            if song:
                queue_len = len(player.get_queue())
                if queue_len > 0 and (player.is_playing or player.current_song != song):
                    await self.send_message(
                        room_id,
                        f"➕ **Added to queue** (position #{queue_len}):\n"
                        f"> 🎵 **[{song.title}]({song.webpage_url})**\n"
                        f"⏱️ Duration: `{song.duration_str}` | Uploader: `{song.uploader}`"
                    )
            else:
                await self.send_message(
                    room_id,
                    f"❌ No results found for that query or link:\n`{query}`"
                )
            return

    def _format_seconds(self, seconds: float) -> str:
        secs = int(max(seconds, 0))
        mins, s = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{s:02d}"
        return f"{mins:02d}:{s:02d}"

    async def _handle_bang_command(self, room_id: str, sender: str, raw: str):
        """Dispatches a '!'-prefixed command, usable without mentioning the bot."""
        parts = raw.strip().split(maxsplit=1)
        if not parts:
            return

        cmd_word = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        canonical = BANG_ALIASES.get(cmd_word)
        if not canonical:
            return

        logger.info("[%s] Bang command from %s: '%s' args='%s'", room_id, sender, canonical, args)
        handler = getattr(self, f"_bang_{canonical}")
        try:
            await handler(room_id, sender, args)
        except Exception as e:
            logger.error("[%s] Error handling bang command '%s': %s", room_id, canonical, e)

    async def _bang_help(self, room_id: str, sender: str, args: str):
        await self.send_message(room_id, BANG_HELP_TEXT)

    async def _bang_join(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        if player.voice_client.is_connected:
            await self.send_message(room_id, "✅ Already connected to the call in this room.")
            return
        await self.send_message(room_id, "📻 Joining the Element Call...")
        ok = await player.join_call()
        if ok:
            await self.send_message(room_id, "✅ Joined the call.")
        else:
            await self.send_message(room_id, "❌ Failed to join the call. Make sure a call is active in this room.")

    async def _bang_leave(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        await player.leave()
        await self.send_message(room_id, "👋 Left the call and cleared the queue.")

    async def _bang_play(self, room_id: str, sender: str, args: str):
        query = args.strip()
        if not query:
            await self.send_message(room_id, "⚠️ Usage: `!play <url-or-query>`")
            return

        player = await self.queue_manager.get_player(room_id)

        if player.resolver.is_playlist_url(query):
            await self.send_message(room_id, f"📃 Playlist detected, reading tracks: `{query}`...")
            count = await player.add_playlist(query, requested_by=sender)
            if count:
                await self.send_message(
                    room_id, f"➕ Found {count} track(s) in the playlist. Adding them to the queue..."
                )
            else:
                await self.send_message(room_id, "❌ Could not read that playlist.")
            return

        await self.send_message(room_id, f"🔍 Searching: `{query}`...")
        song = await player.add_query(query, requested_by=sender)
        if song:
            queue_len = len(player.get_queue())
            if queue_len > 0 and (player.is_playing or player.current_song != song):
                await self.send_message(
                    room_id,
                    f"➕ Added to queue (position #{queue_len}): **{song.title}** (`{song.duration_str}`)"
                )
        else:
            await self.send_message(room_id, f"❌ No results found for: `{query}`")

    async def _bang_queue(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        queue = player.get_queue()
        curr = player.current_song
        if not curr and not queue:
            await self.send_message(room_id, "📭 The queue is empty.")
            return

        loop_note = ""
        if player.loop_enabled:
            loop_note = " (🔂 track loop on)"
        elif player.loop_queue_enabled:
            loop_note = " (🔁 queue loop on)"
        lines = [f"**Queue:**{loop_note}"]
        eta_seconds = 0.0
        if curr:
            streamer = player.voice_client.audio_streamer
            elapsed = (streamer.frames_streamed * FRAME_DURATION_MS / 1000) if streamer else 0
            eta_seconds = max((curr.duration or 0) - elapsed, 0)
            status = "⏸️ paused" if player.is_paused else "▶️ playing"
            lines.append(f"**Now:** [{status}] {curr.title} (`{curr.duration_str}`)")

        if queue:
            lines.append("")
            for i, s in enumerate(queue, 1):
                lines.append(
                    f"{i}. {s.title} (`{s.duration_str}`) — ETA `{self._format_seconds(eta_seconds)}` — by {s.requested_by}"
                )
                eta_seconds += s.duration or 0

        await self.send_message(room_id, "\n".join(lines))

    async def _bang_nowplaying(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        curr = player.current_song
        if not curr:
            await self.send_message(room_id, "💤 Nothing is playing right now.")
            return
        status = "⏸️ paused" if player.is_paused else "▶️ playing"
        await self.send_message(
            room_id,
            f"🎵 **Now Playing:** {curr.title}\n"
            f"⏱️ Duration: `{curr.duration_str}` | Uploader: `{curr.uploader}`\n"
            f"Status: {status} | Requested by: `{curr.requested_by}`"
        )

    async def _bang_skip(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        skipped = await player.skip()
        if skipped:
            await self.send_message(room_id, "⏭️ Skipped the current track.")
        else:
            await self.send_message(room_id, "⚠️ Nothing is playing.")

    async def _bang_stop(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        paused = await player.pause()
        if paused:
            await self.send_message(room_id, "⏸️ Stopped where it is. Queue and loop are kept — use `!resume` to continue.")
        else:
            await self.send_message(room_id, "⚠️ Nothing is playing.")

    async def _bang_resume(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        resumed = await player.resume()
        if resumed:
            await self.send_message(room_id, "▶️ Resumed playback.")
        else:
            await self.send_message(room_id, "⚠️ Nothing is stopped/paused.")

    async def _bang_loop(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        enabled = player.toggle_loop()
        if enabled:
            await self.send_message(room_id, "Loop mode enabled 🔁 (repeats the current track).")
        else:
            await self.send_message(room_id, "Loop mode disabled.")

    async def _bang_loopqueue(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        enabled = player.toggle_loop_queue()
        if enabled:
            await self.send_message(room_id, "Queue loop enabled 🔁 (replays the whole queue once it ends).")
        else:
            await self.send_message(room_id, "Queue loop disabled.")

    async def _bang_progress(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        curr = player.current_song
        if not curr:
            await self.send_message(room_id, "💤 Nothing is playing right now.")
            return

        streamer = player.voice_client.audio_streamer
        elapsed = (streamer.frames_streamed * FRAME_DURATION_MS / 1000) if streamer else 0
        duration = curr.duration or 0

        if duration <= 0:
            await self.send_message(
                room_id,
                f"🎵 **{curr.title}**\n"
                f"`🔴 LIVE` — elapsed `{self._format_seconds(elapsed)}`"
            )
            return

        elapsed = min(elapsed, duration)
        width = 20
        filled = int(width * elapsed / duration)
        bar = "█" * filled + "░" * (width - filled)
        icon = "⏸️" if player.is_paused else "▶️"
        await self.send_message(
            room_id,
            f"🎵 **{curr.title}**\n"
            f"`{icon} [{bar}] {self._format_seconds(elapsed)} / {self._format_seconds(duration)}`"
        )

    async def _bang_history(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        history = player.get_history(limit=10)
        if not history:
            await self.send_message(room_id, "📭 No playback history yet.")
            return
        lines = ["**Recent History:**"]
        for i, s in enumerate(history, 1):
            lines.append(f"{i}. {s.title} (`{s.duration_str}`) — requested by {s.requested_by}")
        await self.send_message(room_id, "\n".join(lines))

    async def _bang_save(self, room_id: str, sender: str, args: str):
        parts = args.split()
        force = "--force" in parts
        name = " ".join(p for p in parts if p != "--force").strip()
        if not name:
            await self.send_message(room_id, "⚠️ Usage: `!save <name> [--force]`")
            return

        player = await self.queue_manager.get_player(room_id)
        ok = await player.save_queue(name, force=force)
        if ok:
            await self.send_message(room_id, f"💾 Saved queue as `{name}`.")
        else:
            await self.send_message(room_id, f"⚠️ A saved queue named `{name}` already exists. Use `--force` to overwrite.")

    async def _bang_load(self, room_id: str, sender: str, args: str):
        name = args.strip()
        if not name:
            await self.send_message(room_id, "⚠️ Usage: `!load <name>`")
            return

        player = await self.queue_manager.get_player(room_id)
        count = await player.load_queue(name)
        if count is None:
            await self.send_message(room_id, f"❌ No saved queue named `{name}` found.")
        else:
            await self.send_message(room_id, f"📥 Loaded {count} track(s) from `{name}` into the queue.")

    async def _bang_queues(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        saved = player.list_saved_queues()
        if not saved:
            await self.send_message(room_id, "📭 No saved queues for this room.")
            return
        lines = ["**Saved Queues:**"]
        for q in saved:
            lines.append(f"• `{q['name']}` ({q['count']} track(s))")
        await self.send_message(room_id, "\n".join(lines))

    async def _bang_deletequeue(self, room_id: str, sender: str, args: str):
        name = args.strip()
        if not name:
            await self.send_message(room_id, "⚠️ Usage: `!deletequeue <name>`")
            return

        player = await self.queue_manager.get_player(room_id)
        ok = player.delete_saved_queue(name)
        if ok:
            await self.send_message(room_id, f"🗑️ Deleted saved queue `{name}`.")
        else:
            await self.send_message(room_id, f"❌ No saved queue named `{name}` found.")

    async def _bang_renamequeue(self, room_id: str, sender: str, args: str):
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            await self.send_message(room_id, "⚠️ Usage: `!renamequeue <old> <new>`")
            return

        old_name, new_name = parts[0], parts[1].strip()
        player = await self.queue_manager.get_player(room_id)
        ok = player.rename_saved_queue(old_name, new_name)
        if ok:
            await self.send_message(room_id, f"✏️ Renamed `{old_name}` to `{new_name}`.")
        else:
            await self.send_message(
                room_id,
                f"❌ Could not rename — either `{old_name}` doesn't exist or `{new_name}` already exists."
            )

    async def _bang_audio(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        s = player.audio_settings
        await self.send_message(
            room_id,
            f"🎚️ **Audio Settings:**\n"
            f"Volume: `{s.volume_percent}%`\n"
            f"Normalize: `{'on' if s.normalize else 'off'}`\n"
            f"Fade-in: `{s.fadein_ms}ms`"
        )

    async def _bang_normalize(self, room_id: str, sender: str, args: str):
        val = args.strip().lower()
        if val not in ("on", "off"):
            await self.send_message(room_id, "⚠️ Usage: `!normalize on|off`")
            return

        player = await self.queue_manager.get_player(room_id)
        player.set_normalize(val == "on")
        await self.send_message(room_id, f"Normalization turned `{val}`. Takes effect on the next track.")

    async def _bang_fadein(self, room_id: str, sender: str, args: str):
        try:
            ms = int(args.strip())
        except ValueError:
            await self.send_message(room_id, f"⚠️ Usage: `!fadein <ms>` (0-{MAX_FADEIN_MS})")
            return

        if not (0 <= ms <= MAX_FADEIN_MS):
            await self.send_message(room_id, f"⚠️ Fade-in must be between 0 and {MAX_FADEIN_MS}ms.")
            return

        player = await self.queue_manager.get_player(room_id)
        player.set_fadein(ms)
        await self.send_message(room_id, f"Fade-in set to `{ms}ms`. Takes effect on the next track.")

    async def _bang_volume(self, room_id: str, sender: str, args: str):
        try:
            pct = int(args.strip())
        except ValueError:
            await self.send_message(room_id, f"⚠️ Usage: `!volume <{MIN_VOLUME_PERCENT}-{MAX_VOLUME_PERCENT}>`")
            return

        if not (MIN_VOLUME_PERCENT <= pct <= MAX_VOLUME_PERCENT):
            await self.send_message(room_id, f"⚠️ Volume must be between {MIN_VOLUME_PERCENT} and {MAX_VOLUME_PERCENT}.")
            return

        player = await self.queue_manager.get_player(room_id)
        player.set_volume(pct)
        await self.send_message(room_id, f"🔊 Volume set to `{pct}%`. Takes effect on the next track.")

    async def _bang_status(self, room_id: str, sender: str, args: str):
        status = self.queue_manager.get_status()
        uptime = self._format_seconds(time.monotonic() - self.start_time)
        await self.send_message(
            room_id,
            f"📊 **Bot Status:**\n"
            f"Uptime: `{uptime}`\n"
            f"Active rooms: `{status['total_rooms']}`\n"
            f"Connected calls: `{status['connected_rooms']}`\n"
            f"Currently playing: `{status['playing_rooms']}`\n"
            f"Proxy: `{'configured' if status['proxy_configured'] else 'none'}`"
        )

    async def _bang_diag(self, room_id: str, sender: str, args: str):
        player = await self.queue_manager.get_player(room_id)
        diag = player.get_diag_info()
        ffmpeg_path = shutil.which("ffmpeg") or "not found"
        ytdlp_path = shutil.which("yt-dlp") or "not found"
        await self.send_message(
            room_id,
            f"🔧 **Diagnostics ({room_id}):**\n"
            f"Connected: `{diag['connected']}` | Playing: `{diag['is_playing']}` | Paused: `{diag['is_paused']}`\n"
            f"Loop: `{diag['loop_enabled']}` | Queue Loop: `{diag['loop_queue_enabled']}` | Queue length: `{diag['queue_length']}`\n"
            f"Frames streamed: `{diag['frames_streamed']}` | Last stream success: `{diag['last_stream_success']}`\n"
            f"Current target: `{diag['current_target']}`\n"
            f"Proxy configured: `{diag['proxy_configured']}` | Idle timeout: `{diag['idle_timeout_sec']}s`\n"
            f"ffmpeg: `{ffmpeg_path}` | yt-dlp: `{ytdlp_path}` | Python: `{sys.version.split()[0]}`"
        )

    async def _bang_config(self, room_id: str, sender: str, args: str):
        if not self.app_config:
            await self.send_message(room_id, "⚠️ No config information available.")
            return
        lines = ["**Active Config:**"]
        for key, value in self.app_config.items():
            lines.append(f"`{key}`: `{value}`")
        await self.send_message(room_id, "\n".join(lines))

    async def _bang_defaults(self, room_id: str, sender: str, args: str):
        await self.send_message(
            room_id,
            "**Default Config Values:**\n"
            f"Volume: `{DEFAULT_VOLUME_PERCENT}%`\n"
            f"Normalize: `{'on' if DEFAULT_NORMALIZE else 'off'}`\n"
            f"Fade-in: `{DEFAULT_FADEIN_MS}ms`\n"
            f"Idle leave timeout: `{IDLE_TIMEOUT_SECONDS}s`\n"
            f"History limit: `{HISTORY_LIMIT}`\n"
            f"Sample rate: `{SAMPLE_RATE}Hz` | Frame duration: `{FRAME_DURATION_MS}ms`"
        )
