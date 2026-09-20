"""
Queue and Playback Manager for Matrix Radio Bot
Provides an isolated queue, sequential worker loop, and LiveKit voice client per room.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from bot.livekit_voice import LiveKitVoiceClient
from bot.song_resolver import SongResolver, ResolvedSong

logger = logging.getLogger("radio.queue")


class RoomPlayer:
    """Manages the music queue and playback worker for a single Matrix room."""

    def __init__(self, room_id: str, homeserver_url: str, user_id: str, access_token: str,
                 jwt_service_url: str, sfu_url: str, send_message_callback,
                 proxy_url: Optional[str] = None):
        self.room_id = room_id
        self.send_message = send_message_callback
        self.resolver = SongResolver(proxy_url=proxy_url)

        self.voice_client = LiveKitVoiceClient(
            homeserver_url=homeserver_url,
            user_id=user_id,
            access_token=access_token,
            jwt_service_url=jwt_service_url,
            sfu_url=sfu_url,
            proxy_url=proxy_url
        )

        self._queue: List[ResolvedSong] = []
        self._current_song: Optional[ResolvedSong] = None
        self._queue_lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._skip_event = asyncio.Event()

    @property
    def current_song(self) -> Optional[ResolvedSong]:
        return self._current_song

    @property
    def is_playing(self) -> bool:
        if self.voice_client and self.voice_client.audio_streamer:
            return self.voice_client.audio_streamer.is_playing
        return False

    @property
    def is_paused(self) -> bool:
        if self.voice_client and self.voice_client.audio_streamer:
            return self.voice_client.audio_streamer.is_paused
        return False

    def get_queue(self) -> List[ResolvedSong]:
        return list(self._queue)

    async def add_query(self, query: str, requested_by: str) -> Optional[ResolvedSong]:
        """Resolves the song and enqueues it."""
        song = await self.resolver.resolve(query, requested_by)
        if not song:
            return None

        async with self._queue_lock:
            self._queue.append(song)
            pos = len(self._queue)

        logger.info("[%s] Enqueued song: %s (Position: %d)", self.room_id, song.title, pos)

        # Start worker if not running
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

        return song

    async def pause(self) -> bool:
        if self.voice_client.audio_streamer and self.voice_client.audio_streamer.is_playing:
            await self.voice_client.audio_streamer.pause()
            return True
        return False

    async def resume(self) -> bool:
        if self.voice_client.audio_streamer and self.voice_client.audio_streamer.is_paused:
            await self.voice_client.audio_streamer.resume()
            return True
        return False

    async def skip(self) -> bool:
        if self.voice_client.audio_streamer and self.voice_client.audio_streamer.is_playing:
            self._skip_event.set()
            await self.voice_client.audio_streamer.stop()
            return True
        return False

    async def leave(self):
        """Clears queue, stops music, leaves voice call."""
        async with self._queue_lock:
            self._queue.clear()

        self._skip_event.set()

        if self.voice_client.audio_streamer:
            await self.voice_client.audio_streamer.stop()

        await self.voice_client.leave()

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        self._current_song = None
        self._worker_task = None
        logger.info("[%s] Room player stopped and left call.", self.room_id)

    async def _worker_loop(self):
        """Sequential queue processing worker for this specific room."""
        logger.info("[%s] Playback worker loop started.", self.room_id)
        try:
            while True:
                song: Optional[ResolvedSong] = None
                async with self._queue_lock:
                    if self._queue:
                        song = self._queue.pop(0)

                if song is None:
                    logger.info("[%s] Queue is empty. Waiting idle timeout...", self.room_id)
                    await asyncio.sleep(5)
                    async with self._queue_lock:
                        if not self._queue:
                            logger.info("[%s] Still empty after idle grace period. Leaving call.", self.room_id)
                            await self.send_message(
                                self.room_id,
                                "⏹️ **پایان صف آهنگ‌ها.** خروج از تماس صوتی."
                            )
                            await self.voice_client.leave()
                            self._current_song = None
                            break
                        else:
                            continue

                self._current_song = song
                self._skip_event.clear()

                # Ensure voice client is connected
                if not self.voice_client.is_connected:
                    await self.send_message(
                        self.room_id,
                        f"📻 در حال اتصال به تماس صوتی برای پخش: **{song.title}**..."
                    )
                    connected = await self.voice_client.join(self.room_id)
                    if not connected:
                        await self.send_message(
                            self.room_id,
                            "❌ **خطا در اتصال به تماس صوتی.** لطفاً مطمئن شوید کال در این روم آغاز شده است."
                        )
                        continue

                source_badge = {
                    "youtube": "🔴 YouTube",
                    "soundcloud": "🟠 SoundCloud",
                    "spotify": "🟢 Spotify (YouTube Match)",
                    "search": "🔍 YouTube Search",
                    "direct": "📁 Direct Audio"
                }.get(song.source_type, "🎵 Music")

                await self.send_message(
                    self.room_id,
                    f"🎶 **هم‌اکنون در حال پخش:**\n"
                    f"> 🎵 **[{song.title}]({song.webpage_url})**\n"
                    f"⏱️ زمان: `{song.duration_str}` | 👤 خواننده: `{song.uploader}`\n"
                    f"🏷️ منبع: `{source_badge}` | 👤 درخواست: `{song.requested_by}`"
                )

                started = await self.voice_client.audio_streamer.play(song.target, is_direct=song.is_direct)
                if not started:
                    await self.send_message(
                        self.room_id,
                        f"❌ خطا در باز کردن استریم آهنگ: {song.title}"
                    )
                    continue

                # Wait while playing
                while self.voice_client.audio_streamer.is_playing:
                    await asyncio.sleep(1)
                    if self._skip_event.is_set():
                        logger.info("[%s] Track skipped.", self.room_id)
                        break

                logger.info("[%s] Finished playing track: %s", self.room_id, song.title)

        except asyncio.CancelledError:
            logger.info("[%s] Playback worker task cancelled.", self.room_id)
        except Exception as e:
            logger.error("[%s] Unexpected error in playback worker: %s", self.room_id, e)
        finally:
            self._current_song = None
            self._worker_task = None


class QueueManager:
    """Registry maintaining separate RoomPlayers for each room/chat."""

    def __init__(self, homeserver_url: str, user_id: str, access_token: str,
                 jwt_service_url: str, sfu_url: str, send_message_callback,
                 proxy_url: Optional[str] = None):
        self.homeserver_url = homeserver_url
        self.user_id = user_id
        self.access_token = access_token
        self.jwt_service_url = jwt_service_url
        self.sfu_url = sfu_url
        self.send_message = send_message_callback
        self.proxy_url = proxy_url

        self._players: Dict[str, RoomPlayer] = {}
        self._lock = asyncio.Lock()

    async def get_player(self, room_id: str) -> RoomPlayer:
        """Retrieves or instantiates the isolated RoomPlayer for room_id."""
        async with self._lock:
            if room_id not in self._players:
                self._players[room_id] = RoomPlayer(
                    room_id=room_id,
                    homeserver_url=self.homeserver_url,
                    user_id=self.user_id,
                    access_token=self.access_token,
                    jwt_service_url=self.jwt_service_url,
                    sfu_url=self.sfu_url,
                    send_message_callback=self.send_message,
                    proxy_url=self.proxy_url
                )
            return self._players[room_id]

    async def cleanup_all(self):
        """Leaves all active voice calls cleanly on bot shutdown."""
        async with self._lock:
            for player in self._players.values():
                await player.leave()
            self._players.clear()
