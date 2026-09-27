"""
Song Resolver for Matrix Radio Bot
Resolves YouTube, SoundCloud, Spotify, direct audio URLs, and text search queries.
Uses yt-dlp and Spotify oEmbed metadata extraction.
"""

import asyncio
import logging
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp

logger = logging.getLogger("radio.resolver")

DIRECT_AUDIO_EXTENSIONS = (".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac", ".m4a", ".m3u8")
PLAYLIST_ENTRY_LIMIT = 50


@dataclass
class ResolvedSong:
    title: str
    target: str
    webpage_url: str
    uploader: str
    duration: int
    duration_str: str
    source_type: str
    requested_by: str
    is_direct: bool = False


class SongResolver:
    def __init__(self, proxy_url: Optional[str] = None):
        self.proxy_url = proxy_url

    def _format_duration(self, seconds: Optional[float]) -> str:
        if not seconds:
            return "Live / Unknown"
        secs = int(seconds)
        mins, s = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{s:02d}"
        return f"{mins:02d}:{s:02d}"

    def _resolve_spotify_metadata(self, spotify_url: str) -> Optional[str]:
        """Extracts track title and artist from Spotify without needing API credentials."""
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={spotify_url}"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title", "").strip()

            # Try to fetch artist from page meta description
            artist = ""
            try:
                page_req = urllib.request.Request(spotify_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(page_req, timeout=6) as page_resp:
                    html = page_resp.read().decode("utf-8", errors="ignore")
                    m = re.search(r'<meta property="og:description" content="([^·"]+)', html)
                    if m:
                        artist = m.group(1).strip()
            except Exception:
                pass

            if artist and title:
                return f"{artist} - {title}"
            return title or None
        except Exception as e:
            logger.warning("Failed to extract Spotify metadata: %s", e)
            return None

    def is_playlist_url(self, query: str) -> bool:
        """Cheap, network-free check for whether a URL points at a playlist/album/set."""
        query = query.strip()
        parsed = urlparse(query)
        if not (parsed.scheme and parsed.netloc):
            return False

        netloc = parsed.netloc.lower()
        path = parsed.path.lower()

        if "open.spotify.com" in netloc:
            return path.startswith("/playlist/") or path.startswith("/album/")

        if "soundcloud.com" in netloc:
            return "/sets/" in path

        if "youtube.com" in netloc or "youtu.be" in netloc:
            qs = parse_qs(parsed.query)
            # A bare playlist link (?list=...). A watch link that also carries a
            # `list=` param (e.g. autoplay context) is treated as a single video.
            return path.rstrip("/") == "/playlist" and "list" in qs

        return False

    def _resolve_spotify_playlist_queries(self, url: str) -> Optional[List[str]]:
        """Scrapes the Spotify embed page for a playlist/album's track list (no API key needed)."""
        m = re.search(r"open\.spotify\.com/(playlist|album)/([A-Za-z0-9]+)", url)
        if not m:
            return None

        kind, spotify_id = m.group(1), m.group(2)
        embed_url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
        try:
            req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            if not m2:
                return None

            data = json.loads(m2.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            track_list = entity.get("trackList") or []

            queries: List[str] = []
            for track in track_list[:PLAYLIST_ENTRY_LIMIT]:
                title = re.sub(r"\s+", " ", (track.get("title") or "")).strip()
                artist = re.sub(r"\s+", " ", (track.get("subtitle") or "")).strip()
                if title and artist:
                    queries.append(f"{artist} - {title}")
                elif title:
                    queries.append(title)

            return queries or None
        except Exception as e:
            logger.warning("Failed to extract Spotify playlist tracks: %s", e)
            return None

    def _extract_flat_playlist_queries(self, url: str) -> Optional[List[str]]:
        """Lists a YouTube playlist or SoundCloud set's tracks without fully resolving each one."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "playlistend": PLAYLIST_ENTRY_LIMIT,
        }
        if self.proxy_url:
            ydl_opts["proxy"] = self.proxy_url

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return None

            entries = info.get("entries") or []
            queries: List[str] = []
            for entry in entries:
                if not entry:
                    continue
                entry_url = entry.get("url")
                if entry_url and entry_url.startswith("http"):
                    queries.append(entry_url)
                elif entry.get("id"):
                    queries.append(f"https://www.youtube.com/watch?v={entry['id']}")

            return queries[:PLAYLIST_ENTRY_LIMIT] or None
        except Exception as e:
            logger.error("Error extracting playlist entries for '%s': %s", url, e)
            return None

    def _resolve_playlist_queries_sync(self, url: str) -> Optional[List[str]]:
        parsed = urlparse(url)
        if "open.spotify.com" in parsed.netloc.lower():
            return self._resolve_spotify_playlist_queries(url)
        return self._extract_flat_playlist_queries(url)

    async def resolve_playlist(self, url: str) -> Optional[List[str]]:
        """Asynchronously lists a playlist's tracks as individual resolvable queries/URLs."""
        return await asyncio.to_thread(self._resolve_playlist_queries_sync, url)

    def _extract_sync(self, query: str, requested_by: str) -> Optional[ResolvedSong]:
        query = query.strip()
        parsed = urlparse(query)
        is_url = bool(parsed.scheme and parsed.netloc)

        # 1. Direct audio file URL
        if is_url and any(parsed.path.lower().endswith(ext) for ext in DIRECT_AUDIO_EXTENSIONS):
            filename = parsed.path.split("/")[-1] or "Audio Stream"
            return ResolvedSong(
                title=filename,
                target=query,
                webpage_url=query,
                uploader="Direct Audio",
                duration=0,
                duration_str="Live / Direct",
                source_type="direct",
                requested_by=requested_by,
                is_direct=True
            )

        source_type = "youtube"
        target_query = query

        # 2. Spotify Track URL
        if "open.spotify.com/track/" in query:
            source_type = "spotify"
            meta_query = self._resolve_spotify_metadata(query)
            if meta_query:
                logger.info("Resolved Spotify URL to search term: '%s'", meta_query)
                target_query = f"ytsearch1:{meta_query}"
            else:
                logger.warning("Could not resolve Spotify metadata, falling back to search")
                target_query = f"ytsearch1:{query}"

        # 3. SoundCloud
        elif "soundcloud.com" in query:
            source_type = "soundcloud"

        # 4. YouTube URL or Plain Text Search
        elif is_url and any(domain in parsed.netloc for domain in ("youtube.com", "youtu.be")):
            source_type = "youtube"
        elif not is_url:
            source_type = "search"
            if not query.startswith("ytsearch"):
                target_query = f"ytsearch1:{query}"

        # Configure yt-dlp
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        if self.proxy_url:
            ydl_opts["proxy"] = self.proxy_url

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_query, download=False)
                if not info:
                    return None

                if "entries" in info and info["entries"]:
                    entry = info["entries"][0]
                else:
                    entry = info

                title = entry.get("title", "Unknown Title")
                duration = int(entry.get("duration") or 0)
                uploader = entry.get("uploader") or entry.get("channel") or "Unknown Artist"
                webpage_url = entry.get("webpage_url") or (query if is_url else f"https://www.youtube.com/watch?v={entry.get('id')}")
                target = entry.get("webpage_url") or entry.get("url") or target_query

                return ResolvedSong(
                    title=title,
                    target=target,
                    webpage_url=webpage_url,
                    uploader=uploader,
                    duration=duration,
                    duration_str=self._format_duration(duration),
                    source_type=source_type,
                    requested_by=requested_by,
                    is_direct=False
                )
        except Exception as e:
            logger.error("Error extracting info for '%s': %s", query, e)
            return None

    async def resolve(self, query: str, requested_by: str) -> Optional[ResolvedSong]:
        """Asynchronously resolves a song query in a worker thread."""
        return await asyncio.to_thread(self._extract_sync, query, requested_by)
