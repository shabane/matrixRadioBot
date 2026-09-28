"""
Queue Manager for Matrix Radio Bot
Manages per-room playback queues, sequential song playback, and voice client lifecycle.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from bot.song_resolver import SongResolver, ResolvedSong
from bot.livekit_voice import LiveKitVoiceClient
from bot.audio_settings import AudioSettings
from bot.storage import QueueStore, HistoryStore, LiveStateStore

logger = logging.getLogger("radio.queue")

IDLE_TIMEOUT_SECONDS = 60
PLAYLIST_RESOLVE_CONCURRENCY = 4

_queue_store = QueueStore()
_history_store = HistoryStore()
_live_state_store = LiveStateStore()


class RoomPlayer:
    """Manages the music queue and playback worker for a specific Matrix room."""

    def __init__(self, room_id: str, homeserver_url: str, user_id: str, access_token: str,
                 jwt_service_url: str, sfu_url: str, send_message_callback,
                 proxy_url: Optional[str] = None, idle_timeout_sec: int = IDLE_TIMEOUT_SECONDS):
        self.room_id = room_id
        self.send_message = send_message_callback
        self.idle_timeout_sec = idle_timeout_sec

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
        self._worker_task: Optional[asyncio.Task] = None
        self._queue_lock = asyncio.Lock()
        self._skip_event = asyncio.Event()

        self.audio_settings = AudioSettings()
        self.loop_enabled = False
        self.loop_queue_enabled = False
        self._history: List[ResolvedSong] = _history_store.load(room_id)

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

    def _persist_live_state(self):
        """Snapshots the current song + remaining queue + loop flags so a restart can resume it."""
        songs = ([self._current_song] if self._current_song else []) + list(self._queue)
        if songs:
            _live_state_store.save(self.room_id, songs, self.loop_enabled, self.loop_queue_enabled)
        else:
            _live_state_store.clear(self.room_id)

    async def restore_live_state(self, songs: List[ResolvedSong], loop_enabled: bool, loop_queue_enabled: bool = False):
        """Re-enqueues tracks left over from before a restart (e.g. a redeploy) and resumes them."""
        async with self._queue_lock:
            self._queue.extend(songs)
        self.loop_enabled = loop_enabled
        self.loop_queue_enabled = loop_queue_enabled

        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def add_query(self, query: str, requested_by: str) -> Optional[ResolvedSong]:
        """Resolves the song and enqueues it."""
        song = await self.resolver.resolve(query, requested_by)
        if not song:
            return None

        async with self._queue_lock:
            self._queue.append(song)
            pos = len(self._queue)
        self._persist_live_state()

        logger.info("[%s] Enqueued song: %s (Position: %d)", self.room_id, song.title, pos)

        # Start worker if not running
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

        return song

    async def add_playlist(self, url: str, requested_by: str) -> Optional[int]:
        """Lists a playlist URL's tracks and enqueues them progressively in the background.

        Returns the number of tracks found (not yet all resolved), or None if the
        playlist could not be read at all.
        """
        queries = await self.resolver.resolve_playlist(url)
        if not queries:
            return None

        asyncio.create_task(self._enqueue_playlist(queries, requested_by))
        return len(queries)

    async def _enqueue_playlist(self, queries: List[str], requested_by: str):
        """Resolves playlist tracks with bounded concurrency and enqueues them in original order,
        starting playback as soon as the first one is ready. Resolving strictly one-at-a-time was
        too slow for longer playlists — each track needs its own yt-dlp network round trip, so
        !queue would look like it only had one song in it for a long time after !play."""
        semaphore = asyncio.Semaphore(PLAYLIST_RESOLVE_CONCURRENCY)

        async def resolve_one(query: str) -> Optional[ResolvedSong]:
            async with semaphore:
                return await self.resolver.resolve(query, requested_by)

        tasks = [asyncio.create_task(resolve_one(q)) for q in queries]

        added = 0
        failed = 0
        for task in tasks:
            song = await task
            if not song:
                failed += 1
                continue

            async with self._queue_lock:
                self._queue.append(song)
            added += 1
            self._persist_live_state()

            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(self._worker_loop())

        logger.info("[%s] Playlist enqueue finished: %d added, %d failed", self.room_id, added, failed)
        if added and not failed:
            await self.send_message(self.room_id, f"✅ Finished adding playlist: {added} track(s) queued.")
        elif added and failed:
            await self.send_message(
                self.room_id, f"✅ Finished adding playlist: {added} track(s) queued, {failed} failed."
            )
        elif failed:
            await self.send_message(self.room_id, "❌ Failed to add any tracks from that playlist.")

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
        self.loop_enabled = False
        self.loop_queue_enabled = False
        self._persist_live_state()
        logger.info("[%s] Room player stopped and left call.", self.room_id)

    async def join_call(self) -> bool:
        """Explicitly connects to the room's voice call without playing anything."""
        if self.voice_client.is_connected:
            return True
        return await self.voice_client.join(self.room_id)

    async def shutdown(self):
        """Stops playback for process exit WITHOUT clearing the queue, so restore_live_state()
        can resume it after the process restarts (e.g. a redeploy). Unlike leave(), this
        intentionally leaves the already-persisted live state file in place."""
        if self.voice_client.audio_streamer:
            await self.voice_client.audio_streamer.stop()

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        await self.voice_client.leave()

    def toggle_loop(self) -> bool:
        """Toggles repeat-current-track mode. Mutually exclusive with queue-loop. Returns the new state."""
        self.loop_enabled = not self.loop_enabled
        if self.loop_enabled:
            self.loop_queue_enabled = False
        self._persist_live_state()
        return self.loop_enabled

    def toggle_loop_queue(self) -> bool:
        """Toggles repeat-whole-queue mode. Mutually exclusive with single-track loop. Returns the new state."""
        self.loop_queue_enabled = not self.loop_queue_enabled
        if self.loop_queue_enabled:
            self.loop_enabled = False
        self._persist_live_state()
        return self.loop_queue_enabled

    def get_history(self, limit: int = 10) -> List[ResolvedSong]:
        """Returns the most recently played tracks, newest first."""
        return list(reversed(self._history[-limit:]))

    def set_volume(self, percent: int):
        self.audio_settings.volume_percent = percent

    def set_normalize(self, enabled: bool):
        self.audio_settings.normalize = enabled

    def set_fadein(self, ms: int):
        self.audio_settings.fadein_ms = ms

    async def save_queue(self, name: str, force: bool = False) -> bool:
        """Saves the current track plus upcoming queue under a name."""
        songs: List[ResolvedSong] = []
        if self._current_song:
            songs.append(self._current_song)
        async with self._queue_lock:
            songs.extend(self._queue)
        return _queue_store.save(self.room_id, name, songs, force=force)

    async def load_queue(self, name: str) -> Optional[int]:
        """Loads a saved queue and appends it to the current queue. Returns song count, or None if not found."""
        songs = _queue_store.load(self.room_id, name)
        if songs is None:
            return None

        async with self._queue_lock:
            self._queue.extend(songs)
        self._persist_live_state()

        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

        return len(songs)

    def list_saved_queues(self) -> List[dict]:
        return _queue_store.list(self.room_id)

    def delete_saved_queue(self, name: str) -> bool:
        return _queue_store.delete(self.room_id, name)

    def rename_saved_queue(self, old_name: str, new_name: str) -> bool:
        return _queue_store.rename(self.room_id, old_name, new_name)

    def get_diag_info(self) -> dict:
        streamer = self.voice_client.audio_streamer
        return {
            "room_id": self.room_id,
            "connected": self.voice_client.is_connected,
            "is_playing": self.is_playing,
            "is_paused": self.is_paused,
            "loop_enabled": self.loop_enabled,
            "loop_queue_enabled": self.loop_queue_enabled,
            "queue_length": len(self._queue),
            "frames_streamed": streamer.frames_streamed if streamer else 0,
            "last_stream_success": streamer.last_stream_success if streamer else None,
            "current_target": streamer.current_target if streamer else None,
            "proxy_configured": bool(self.resolver.proxy_url),
            "idle_timeout_sec": self.idle_timeout_sec,
        }

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
                    logger.info("[%s] Queue is empty. Waiting idle grace period (%ds)...", self.room_id, self.idle_timeout_sec)
                    idle_waited = 0
                    has_new_song = False
                    while idle_waited < self.idle_timeout_sec:
                        await asyncio.sleep(2)
                        idle_waited += 2
                        async with self._queue_lock:
                            if self._queue:
                                has_new_song = True
                                break

                    if not has_new_song:
                        logger.info("[%s] Still empty after %ds idle grace period. Leaving call.", self.room_id, self.idle_timeout_sec)
                        await self.send_message(
                            self.room_id,
                            f"⏹️ **Queue finished.** Leaving the voice call due to inactivity ({self.idle_timeout_sec}s)."
                        )
                        await self.voice_client.leave()
                        self._current_song = None
                        self.loop_enabled = False
                        self.loop_queue_enabled = False
                        self._persist_live_state()
                        break
                    else:
                        continue

                self._current_song = song
                self._skip_event.clear()
                self._persist_live_state()

                # Ensure voice client is connected
                if not self.voice_client.is_connected:
                    await self.send_message(
                        self.room_id,
                        f"📻 Connecting to the voice call to play: **{song.title}**..."
                    )
                    connected = await self.voice_client.join(self.room_id)
                    if not connected:
                        await self.send_message(
                            self.room_id,
                            "❌ **Failed to connect to the voice call.** Make sure a call has been started in this room."
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
                    f"🎶 **Now Playing:**\n"
                    f"> 🎵 **[{song.title}]({song.webpage_url})**\n"
                    f"⏱️ Duration: `{song.duration_str}` | 👤 Uploader: `{song.uploader}`\n"
                    f"🏷️ Source: `{source_badge}` | 👤 Requested by: `{song.requested_by}`"
                )

                max_retries = 2
                stream_ok = False
                for attempt in range(1, max_retries + 1):
                    started = await self.voice_client.audio_streamer.play(
                        song.target, is_direct=song.is_direct, audio_settings=self.audio_settings
                    )
                    if not started:
                        logger.warning(
                            "[%s] Failed to start audio streamer on attempt %d/%d for %s",
                            self.room_id, attempt, max_retries, song.title
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(1.5)
                        continue

                    # Wait while playing
                    while self.voice_client.audio_streamer.is_playing:
                        await asyncio.sleep(1)
                        if self._skip_event.is_set():
                            logger.info("[%s] Track skipped.", self.room_id)
                            break

                    if self._skip_event.is_set():
                        stream_ok = True
                        break

                    if self.voice_client.audio_streamer.last_stream_success:
                        stream_ok = True
                        break
                    else:
                        logger.warning(
                            "[%s] Track %s aborted early on attempt %d/%d.",
                            self.room_id, song.title, attempt, max_retries
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(2.0)

                track_was_skipped = self._skip_event.is_set()

                if track_was_skipped and self.loop_enabled:
                    self.loop_enabled = False
                    self._persist_live_state()
                    await self.send_message(
                        self.room_id, "🔁 **Loop mode disabled because the track was skipped.**"
                    )

                if not stream_ok and not track_was_skipped:
                    await self.send_message(
                        self.room_id,
                        f"⚠️ **Playback error:** couldn't stream «{song.title}». Moving to the next track..."
                    )

                if stream_ok:
                    try:
                        self._history = _history_store.append(self.room_id, song)
                    except Exception as e:
                        logger.error("[%s] Failed to persist playback history: %s", self.room_id, e)

                if stream_ok and not track_was_skipped and self.loop_enabled:
                    async with self._queue_lock:
                        self._queue.insert(0, song)
                    self._persist_live_state()
                elif stream_ok and not track_was_skipped and self.loop_queue_enabled:
                    async with self._queue_lock:
                        self._queue.append(song)
                    self._persist_live_state()

                logger.info(
                    "[%s] Finished processing track: %s (stream_ok=%s)",
                    self.room_id, song.title, stream_ok
                )

                # Settle pause between consecutive tracks
                await asyncio.sleep(0.5)

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
                 proxy_url: Optional[str] = None, idle_timeout_sec: int = IDLE_TIMEOUT_SECONDS):
        self.homeserver_url = homeserver_url
        self.user_id = user_id
        self.access_token = access_token
        self.jwt_service_url = jwt_service_url
        self.sfu_url = sfu_url
        self.send_message = send_message_callback
        self.proxy_url = proxy_url
        self.idle_timeout_sec = idle_timeout_sec

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
                    proxy_url=self.proxy_url,
                    idle_timeout_sec=self.idle_timeout_sec
                )
            return self._players[room_id]

    async def cleanup_all(self):
        """Stops all active voice calls cleanly on process exit, preserving each room's queue
        (via its already-persisted live state) so it can resume on the next start."""
        async with self._lock:
            for player in self._players.values():
                await player.shutdown()
            self._players.clear()

    async def restore_all_live_state(self):
        """Resumes any rooms that had an in-flight queue when the process last stopped
        (e.g. a redeploy or crash)."""
        for state in _live_state_store.load_all():
            room_id = state["room_id"]
            songs = state["songs"]

            # Clear the on-disk snapshot before resuming: once the worker loop starts consuming
            # the restored queue it will persist fresh state of its own, and clearing afterwards
            # would race with (and could clobber) that fresh write.
            _live_state_store.clear(room_id)
            if not songs:
                continue

            try:
                player = await self.get_player(room_id)
                await player.restore_live_state(songs, state["loop_enabled"], state["loop_queue_enabled"])
                await self.send_message(
                    room_id,
                    f"🔄 **Bot restarted.** Resuming the queue where it left off ({len(songs)} track(s))..."
                )
                logger.info("[%s] Restored %d track(s) from live state.", room_id, len(songs))
            except Exception as e:
                logger.error("Failed to restore live state for room %s: %s", room_id, e)

    def get_status(self) -> dict:
        """Returns a snapshot of bot-wide playback status across all rooms."""
        active_rooms = list(self._players.values())
        connected_rooms = sum(1 for p in active_rooms if p.voice_client.is_connected)
        playing_rooms = sum(1 for p in active_rooms if p.is_playing)
        return {
            "total_rooms": len(active_rooms),
            "connected_rooms": connected_rooms,
            "playing_rooms": playing_rooms,
            "proxy_configured": bool(self.proxy_url),
        }
