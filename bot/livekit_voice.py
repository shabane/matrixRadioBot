"""
LiveKit Voice Client for Matrix Call Integration
Connects to MatrixRTC / LiveKit room, manages AudioTrack and streams music.
"""

import asyncio
import logging
from typing import Optional
import aiohttp

import livekit.rtc as rtc
from bot.audio_streamer import AudioStreamer

logger = logging.getLogger("radio.voice")


class LiveKitVoiceClient:
    def __init__(self, homeserver_url: str, user_id: str, access_token: str,
                 jwt_service_url: str, sfu_url: str, proxy_url: Optional[str] = None):
        self.homeserver_url = homeserver_url.rstrip("/")
        self.user_id = user_id
        self.access_token = access_token
        self.jwt_service_url = jwt_service_url
        self.sfu_url = sfu_url
        self.proxy_url = proxy_url

        self.room: Optional[rtc.Room] = None
        self.audio_source: Optional[rtc.AudioSource] = None
        self.audio_track: Optional[rtc.LocalAudioTrack] = None
        self.audio_streamer: Optional[AudioStreamer] = None
        self.matrix_room_id: Optional[str] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self.room is not None

    async def _get_matrix_openid_token(self) -> dict:
        """Fetches OpenID token from Matrix Synapse."""
        url = f"{self.homeserver_url}/_matrix/client/v3/user/{self.user_id}/openid/request_token"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={}, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Failed to get OpenID token (HTTP {resp.status}): {text}")
                return await resp.json()

    async def _get_livekit_token(self, matrix_room_id: str) -> dict:
        """Exchanges OpenID token with lk-jwt-service for LiveKit JWT."""
        openid_token = await self._get_matrix_openid_token()
        payload = {
            "room": matrix_room_id,
            "openid_token": openid_token
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self.jwt_service_url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Failed to get LiveKit JWT (HTTP {resp.status}): {text}")
                return await resp.json()

    async def _publish_call_member(self, matrix_room_id: str):
        """Publishes MSC3401 call member state to Matrix room so UI shows the bot in the call."""
        state_key = f"_{self.user_id}__m.call"
        content = {
            "application": "m.call",
            "call_id": "",
            "device_id": "",
            "expires": 14400000,
            "foci_preferred": [{
                "livekit_alias": matrix_room_id,
                "livekit_service_url": f"{self.homeserver_url}/livekit/jwt",
                "type": "livekit"
            }],
            "focus_active": {
                "focus_selection": "oldest_membership",
                "type": "livekit"
            },
            "m.call.intent": "audio",
            "membershipID": f"{self.user_id}:",
            "scope": "m.room"
        }
        url = f"{self.homeserver_url}/_matrix/client/v3/rooms/{matrix_room_id}/state/org.matrix.msc3401.call.member/{state_key}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, json=content, headers=headers) as resp:
                    if resp.status in (200, 201):
                        logger.info("Published MSC3401 call.member state to %s", matrix_room_id)
        except Exception as e:
            logger.warning("Failed to publish MSC3401 call.member state: %s", e)

    async def _clear_call_member(self, matrix_room_id: str):
        """Clears MSC3401 call member state from Matrix room when leaving call."""
        state_key = f"_{self.user_id}__m.call"
        url = f"{self.homeserver_url}/_matrix/client/v3/rooms/{matrix_room_id}/state/org.matrix.msc3401.call.member/{state_key}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, json={}, headers=headers) as resp:
                    pass
        except Exception as e:
            logger.warning("Failed to clear MSC3401 call.member state: %s", e)

    async def join(self, matrix_room_id: str) -> bool:
        """Connects to the LiveKit voice room associated with matrix_room_id."""
        if self.is_connected and self.matrix_room_id == matrix_room_id:
            logger.info("Already connected to voice room: %s", matrix_room_id)
            return True

        await self.leave()

        self.matrix_room_id = matrix_room_id
        logger.info("Joining LiveKit voice for Matrix room: %s", matrix_room_id)

        try:
            lk_info = await self._get_livekit_token(matrix_room_id)
            url = lk_info.get("url") or self.sfu_url
            token = lk_info["jwt"]

            self.room = rtc.Room()

            @self.room.on("disconnected")
            def on_disconnected(reason):
                logger.info("LiveKit room disconnected: %s", reason)
                self._connected = False

            logger.info("Connecting to LiveKit SFU: %s", url)
            await self.room.connect(url, token)
            self._connected = True
            logger.info("Connected to LiveKit room: %s", self.room.name)

            # Create audio source and track
            self.audio_source = rtc.AudioSource(sample_rate=48000, num_channels=1)
            self.audio_track = rtc.LocalAudioTrack.create_audio_track("radio-music", self.audio_source)

            # Publish audio track
            publish_opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            pub = await self.room.local_participant.publish_track(self.audio_track, publish_opts)
            logger.info("Published Radio AudioTrack to call! Track SID: %s", pub.sid)

            self.audio_streamer = AudioStreamer(self.audio_source, proxy_url=self.proxy_url)

            # Publish call member state in Matrix room
            await self._publish_call_member(matrix_room_id)

            return True

        except Exception as e:
            logger.error("Failed to join LiveKit voice room: %s", e)
            await self.leave()
            return False

    async def leave(self):
        """Disconnects from the LiveKit voice room and stops playback."""
        if self.audio_streamer:
            await self.audio_streamer.stop()
            self.audio_streamer = None

        if self.matrix_room_id:
            await self._clear_call_member(self.matrix_room_id)

        if self.room:
            try:
                await self.room.disconnect()
            except Exception as e:
                logger.warning("Error disconnecting LiveKit room: %s", e)
            self.room = None

        self.audio_source = None
        self.audio_track = None
        self.matrix_room_id = None
        self._connected = False
        logger.info("Left voice call.")
