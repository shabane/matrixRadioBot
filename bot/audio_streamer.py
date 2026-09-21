"""
Audio Streamer for LiveKit Voice Calls
Streams audio from direct URLs or pipes yt-dlp directly into FFmpeg for raw 48kHz PCM frames.
"""

import os
import asyncio
import logging
from typing import Optional, List
import livekit.rtc as rtc

from bot.audio_settings import AudioSettings

logger = logging.getLogger("radio.audio")

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000))  # 960 samples
BYTES_PER_SAMPLE = 2  # 16-bit signed PCM
CHUNK_SIZE = SAMPLES_PER_FRAME * NUM_CHANNELS * BYTES_PER_SAMPLE   # 1920 bytes


class AudioStreamer:
    def __init__(self, audio_source: rtc.AudioSource, proxy_url: Optional[str] = None):
        self.audio_source = audio_source
        self.proxy_url = proxy_url

        self.ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self.ytdlp_proc: Optional[asyncio.subprocess.Process] = None

        self._is_playing = False
        self._is_paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stream_task: Optional[asyncio.Task] = None
        self._drain_tasks: List[asyncio.Task] = []
        self._ytdlp_stderr = bytearray()
        self._ffmpeg_stderr = bytearray()

        self.current_target: Optional[str] = None
        self.frames_streamed: int = 0
        self.last_stream_success: bool = False
        self.last_error_message: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    async def _drain_stream(self, stream: asyncio.StreamReader, buf: bytearray, max_len: int = 4096):
        """Continuously drains a process stream so OS pipe buffers never fill up."""
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                buf.extend(line)
                if len(buf) > max_len:
                    del buf[:-max_len]
        except (asyncio.CancelledError, Exception):
            pass

    async def play(self, target: str, is_direct: bool = False, audio_settings: Optional[AudioSettings] = None) -> bool:
        """Starts streaming audio to LiveKit AudioSource."""
        await self.stop()

        af_args = audio_settings.build_af_args() if audio_settings else []

        self.current_target = target
        self.frames_streamed = 0
        self.last_stream_success = False
        self.last_error_message = None
        self._ytdlp_stderr.clear()
        self._ffmpeg_stderr.clear()
        self._drain_tasks.clear()

        self._is_playing = True
        self._is_paused = False
        self._pause_event.set()

        try:
            if is_direct:
                logger.info("Streaming direct audio URL with FFmpeg: %s", target)
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-re",
                    "-i", target,
                    "-vn",
                    *af_args,
                    "-f", "s16le",
                    "-ar", str(SAMPLE_RATE),
                    "-ac", str(NUM_CHANNELS),
                    "-loglevel", "warning",
                    "-"
                ]
                self.ffmpeg_proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                if self.ffmpeg_proc.stderr:
                    self._drain_tasks.append(
                        asyncio.create_task(self._drain_stream(self.ffmpeg_proc.stderr, self._ffmpeg_stderr))
                    )
            else:
                logger.info("Streaming via yt-dlp -> FFmpeg pipeline: %s", target)
                ytdlp_cmd = [
                    "yt-dlp",
                    "-f", "bestaudio/best",
                    "-o", "-",
                    "--quiet",
                    "--no-warnings",
                    "--no-playlist",
                    "--socket-timeout", "15",
                    "--retries", "5",
                    "--fragment-retries", "5",
                    "--buffer-size", "16K",
                    target
                ]
                if self.proxy_url:
                    ytdlp_cmd[1:1] = ["--proxy", self.proxy_url]

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-re",
                    "-i", "pipe:0",
                    "-vn",
                    *af_args,
                    "-f", "s16le",
                    "-ar", str(SAMPLE_RATE),
                    "-ac", str(NUM_CHANNELS),
                    "-loglevel", "warning",
                    "-"
                ]

                # Use OS pipe between yt-dlp and ffmpeg
                r_fd, w_fd = os.pipe()

                self.ytdlp_proc = await asyncio.create_subprocess_exec(
                    *ytdlp_cmd,
                    stdout=w_fd,
                    stderr=asyncio.subprocess.PIPE
                )
                os.close(w_fd)  # Close write end in parent

                self.ffmpeg_proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdin=r_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                os.close(r_fd)  # Close read end in parent

                if self.ytdlp_proc.stderr:
                    self._drain_tasks.append(
                        asyncio.create_task(self._drain_stream(self.ytdlp_proc.stderr, self._ytdlp_stderr))
                    )
                if self.ffmpeg_proc.stderr:
                    self._drain_tasks.append(
                        asyncio.create_task(self._drain_stream(self.ffmpeg_proc.stderr, self._ffmpeg_stderr))
                    )

        except Exception as e:
            logger.error("Failed to spawn audio streaming process: %s", e)
            await self.stop()
            return False

        self._stream_task = asyncio.create_task(self._pump_audio())
        return True

    async def _pump_audio(self):
        """Reads PCM chunks from ffmpeg stdout and captures frames into LiveKit."""
        frames_count = 0
        silence_padding = bytes(CHUNK_SIZE)
        try:
            assert self.ffmpeg_proc is not None
            assert self.ffmpeg_proc.stdout is not None

            while self._is_playing:
                await self._pause_event.wait()

                chunk = await self.ffmpeg_proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    logger.info("Audio stream reached EOF after %d frames (%.2fs).", frames_count, frames_count * 0.02)
                    break

                frames_count += 1
                if len(chunk) < CHUNK_SIZE:
                    chunk = chunk + silence_padding[:(CHUNK_SIZE - len(chunk))]

                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=SAMPLE_RATE,
                    num_channels=NUM_CHANNELS,
                    samples_per_channel=SAMPLES_PER_FRAME
                )
                await self.audio_source.capture_frame(frame)

        except asyncio.CancelledError:
            logger.info("Audio pump loop cancelled after %d frames.", frames_count)
        except Exception as e:
            logger.error("Error in audio pump loop: %s", e)
        finally:
            self._is_playing = False
            self.frames_streamed = frames_count

            # Small pause to allow returncodes to populate
            await asyncio.sleep(0.15)
            ytdlp_rc = self.ytdlp_proc.returncode if self.ytdlp_proc else 0
            ffmpeg_rc = self.ffmpeg_proc.returncode if self.ffmpeg_proc else 0

            # Consider stream successful if it streamed at least 100 frames (~2 seconds)
            if frames_count >= 100:
                self.last_stream_success = True
            else:
                self.last_stream_success = False
                logger.warning(
                    "Stream ended prematurely (only %d frames, yt-dlp rc=%s, ffmpeg rc=%s)",
                    frames_count, ytdlp_rc, ffmpeg_rc
                )
                if self._ytdlp_stderr:
                    logger.warning("yt-dlp stderr: %s", self._ytdlp_stderr.decode(errors="replace").strip())
                if self._ffmpeg_stderr:
                    logger.warning("FFmpeg stderr: %s", self._ffmpeg_stderr.decode(errors="replace").strip())

            await self._kill_processes()
            logger.info("Audio stream completed. Success status: %s", self.last_stream_success)

    async def pause(self):
        """Pauses audio streaming."""
        if self._is_playing and not self._is_paused:
            self._is_paused = True
            self._pause_event.clear()
            logger.info("Audio stream paused.")

    async def resume(self):
        """Resumes audio streaming."""
        if self._is_playing and self._is_paused:
            self._is_paused = False
            self._pause_event.set()
            logger.info("Audio stream resumed.")

    async def _kill_processes(self):
        """Cleanly terminates ffmpeg and yt-dlp child processes and drain tasks."""
        for task in self._drain_tasks:
            if not task.done():
                task.cancel()
        self._drain_tasks.clear()

        for proc in (self.ffmpeg_proc, self.ytdlp_proc):
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        self.ffmpeg_proc = None
        self.ytdlp_proc = None

    async def stop(self):
        """Stops playback and cancels tasks."""
        self._is_playing = False
        self._is_paused = False
        self._pause_event.set()

        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        await self._kill_processes()
        self.current_target = None
        logger.info("Audio stream stopped.")
