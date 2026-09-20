"""
Audio Streamer for LiveKit Voice Calls
Streams audio from direct URLs or pipes yt-dlp directly into FFmpeg for raw 48kHz PCM frames.
"""

import os
import asyncio
import logging
from typing import Optional
import livekit.rtc as rtc

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
        self.current_target: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    async def play(self, target: str, is_direct: bool = False) -> bool:
        """Starts streaming audio to LiveKit AudioSource."""
        await self.stop()

        self.current_target = target
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
            else:
                logger.info("Streaming via yt-dlp -> FFmpeg pipeline: %s", target)
                ytdlp_cmd = [
                    "yt-dlp",
                    "-f", "bestaudio/best",
                    "-o", "-",
                    "--quiet",
                    "--no-warnings",
                    target
                ]
                if self.proxy_url:
                    ytdlp_cmd[1:1] = ["--proxy", self.proxy_url]

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-re",
                    "-i", "pipe:0",
                    "-vn",
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

        except Exception as e:
            logger.error("Failed to spawn audio streaming process: %s", e)
            await self.stop()
            return False

        self._stream_task = asyncio.create_task(self._pump_audio())
        return True

    async def _pump_audio(self):
        """Reads PCM chunks from ffmpeg stdout and captures frames into LiveKit."""
        try:
            assert self.ffmpeg_proc is not None
            assert self.ffmpeg_proc.stdout is not None

            while self._is_playing:
                await self._pause_event.wait()

                chunk = await self.ffmpeg_proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    logger.info("Audio stream reached EOF.")
                    break

                if len(chunk) < CHUNK_SIZE:
                    chunk = chunk + b"\x00" * (CHUNK_SIZE - len(chunk))

                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=SAMPLE_RATE,
                    num_channels=NUM_CHANNELS,
                    samples_per_channel=SAMPLES_PER_FRAME
                )
                await self.audio_source.capture_frame(frame)

        except asyncio.CancelledError:
            logger.info("Audio pump loop cancelled.")
        except Exception as e:
            logger.error("Error in audio pump loop: %s", e)
        finally:
            self._is_playing = False
            await self._kill_processes()
            logger.info("Audio stream completed.")

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
        """Cleanly terminates ffmpeg and yt-dlp child processes."""
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
