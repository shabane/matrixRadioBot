"""
Command Handler for Matrix Radio Bot
Parses user commands, URLs (YouTube, SoundCloud, Spotify, Direct), and search queries.
"""

import re
import logging
from typing import Optional
from bot.queue_manager import QueueManager

logger = logging.getLogger("radio.cmd")

URL_REGEX = re.compile(r'https?://[^\s<>"]+')


class CommandHandler:
    def __init__(self, bot_user_id: str, bot_name: str, queue_manager: QueueManager, send_message_callback):
        self.bot_user_id = bot_user_id
        self.bot_name = bot_name.lower()
        self.queue_manager = queue_manager
        self.send_message = send_message_callback

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

        # In group rooms, require mention. In PV/Direct chat, always respond.
        if not is_direct and not self.is_bot_mentioned(text):
            return

        cmd = self.clean_command(text)
        cmd_lower = cmd.lower()
        logger.info("[%s] Command from %s: '%s'", room_id, sender, cmd)

        player = await self.queue_manager.get_player(room_id)

        # 1. Pause
        if cmd_lower in ("pause", "پاز", "توقف"):
            paused = await player.pause()
            if paused:
                await self.send_message(room_id, "⏸️ **پخش موسیقی متوقف شد.** (برای ادامه `@radio resume`)")
            else:
                await self.send_message(room_id, "⚠️ هیچ آهنگی در حال پخش نیست.")
            return

        # 2. Resume
        if cmd_lower in ("resume", "ادامه", "پخش"):
            resumed = await player.resume()
            if resumed:
                await self.send_message(room_id, "▶️ **پخش موسیقی ادامه یافت.**")
            else:
                await self.send_message(room_id, "⚠️ آهنگ در حالت توقف نیست.")
            return

        # 3. Skip / Next
        if cmd_lower in ("skip", "next", "بعدی", "رد"):
            skipped = await player.skip()
            if skipped:
                await self.send_message(room_id, "⏭️ **آهنگ جاری رد شد.** رفتن به آهنگ بعدی...")
            else:
                await self.send_message(room_id, "⚠️ هیچ آهنگی در حال پخش نیست.")
            return

        # 4. Leave / Stop
        if cmd_lower in ("leave", "stop", "قطع", "خروج"):
            await player.leave()
            await self.send_message(room_id, "👋 **صف پاک شد و بات از تماس خارج شد.**")
            return

        # 5. Queue / List
        if cmd_lower in ("queue", "list", "صف", "لیست"):
            queue = player.get_queue()
            curr = player.current_song
            if not curr and not queue:
                await self.send_message(room_id, "📭 **صف پخش این چت خالی است.**")
                return

            msg_lines = ["📋 **لیست صف پخش:**"]
            if curr:
                status = "⏸️ [متوقف]" if player.is_paused else "▶️ [در حال پخش]"
                msg_lines.append(f"**هم‌اکنون:** {status} `[{curr.title}]({curr.webpage_url})` (مدت: `{curr.duration_str}`)")
            if queue:
                msg_lines.append("\n**در صف انتظار:**")
                for i, s in enumerate(queue, 1):
                    msg_lines.append(f"{i}. `[{s.title}]({s.webpage_url})` (مدت: `{s.duration_str}`) | درخواست: `{s.requested_by}`")
            await self.send_message(room_id, "\n".join(msg_lines))
            return

        # 6. Now Playing (np)
        if cmd_lower in ("np", "now", "اهنگ", "آهنگ"):
            curr = player.current_song
            if curr:
                status = "⏸️ متوقف" if player.is_paused else "▶️ در حال پخش"
                await self.send_message(
                    room_id,
                    f"🎵 **آهنگ در حال پخش:**\n"
                    f"> **[{curr.title}]({curr.webpage_url})**\n"
                    f"⏱️ زمان: `{curr.duration_str}` | خواننده: `{curr.uploader}`\n"
                    f"وضعیت: {status}\n👤 درخواست از: `{curr.requested_by}`"
                )
            else:
                await self.send_message(room_id, "💤 هم‌اکنون هیچ آهنگی در حال پخش نیست.")
            return

        # 7. Help
        if cmd_lower in ("help", "راهنما", "?", "دستورات"):
            help_text = (
                "📻 **راهنمای ربات رادیو و پخش موزیک (پشتیبانی از YouTube, Spotify, SoundCloud):**\n\n"
                "• `@radio <لینک>` : پخش از یوتیوب، ساندکلاد، اسپاتیفای یا لینک مستقیم\n"
                "• `@radio play <نام آهنگ>` : جستجو و پخش آهنگ در یوتیوب\n"
                "• `@radio pause` : توقف موقت پخش\n"
                "• `@radio resume` : ادامه پخش\n"
                "• `@radio skip` : رد کردن آهنگ جاری\n"
                "• `@radio queue` : مشاهده صف آهنگ‌ها\n"
                "• `@radio np` : مشاهده اطلاعات آهنگ جاری\n"
                "• `@radio leave` : قطع تماس و پاک کردن صف\n\n"
                "💡 *نکته: در چت خصوصی (PV) نیازی به منشن کردن `@radio` نیست.*"
            )
            await self.send_message(room_id, help_text)
            return

        # 8. Song Playback (URL or Search Query)
        query = cmd
        # Remove play / پخش prefix if present
        for prefix in ("play ", "پخش ", "search ", "سرچ "):
            if query.lower().startswith(prefix):
                query = query[len(prefix):].strip()
                break

        if query:
            await self.send_message(room_id, f"🔍 در حال جستجو و دریافت اطلاعات آهنگ: `{query}`...")
            song = await player.add_query(query, requested_by=sender)
            if song:
                queue_len = len(player.get_queue())
                if queue_len > 0 and (player.is_playing or player.current_song != song):
                    await self.send_message(
                        room_id,
                        f"➕ **آهنگ به صف اضافه شد** (موقعیت #{queue_len} در صف):\n"
                        f"> 🎵 **[{song.title}]({song.webpage_url})**\n"
                        f"⏱️ مدت زمان: `{song.duration_str}` | خواننده: `{song.uploader}`"
                    )
            else:
                await self.send_message(
                    room_id,
                    f"❌ متأسفانه آهنگی برای عبارت یا لینک پیدا نشد:\n`{query}`"
                )
            return
