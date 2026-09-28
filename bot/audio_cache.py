"""
Local disk cache for resolved audio streams.

Looping (single-track or whole-queue) replays the exact same ResolvedSong.target
over and over, and without a cache each replay re-runs the full yt-dlp network
fetch — wasted bandwidth/latency and one more chance to hit a transient 403 from
YouTube. This caches the raw audio yt-dlp fetches (not the final transcoded PCM),
so per-track audio settings (volume/normalize/fade-in) still get re-applied fresh
by ffmpeg on every play, cache hit or not.

Not tied to a persistent volume: this only survives for the lifetime of the
current process/pod, same as the rest of data/. That's fine for its purpose -
avoiding redundant re-fetches within a single listening session.
"""

import logging
import hashlib
import os
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("radio.audiocache")

CACHE_DIR = Path(os.environ.get("RADIO_DATA_DIR", "data")).resolve() / "audio_cache"
MAX_CACHE_BYTES = int(os.environ.get("RADIO_AUDIO_CACHE_MAX_MB", "500")) * 1024 * 1024


def _cache_key(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]


def cached_path(target: str) -> Path:
    return CACHE_DIR / f"{_cache_key(target)}.audio"


def get(target: str) -> Optional[Path]:
    """Returns the cached file for this target, or None if not cached."""
    path = cached_path(target)
    if path.is_file() and path.stat().st_size > 0:
        try:
            path.touch()  # bump mtime for LRU eviction
        except OSError:
            pass
        return path
    return None


def new_tmp_path(target: str) -> Path:
    """A scratch path to tee a fresh download into. Unique per call so concurrent
    fetches of the same target (different rooms) never write the same file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{_cache_key(target)}.{uuid.uuid4().hex[:8]}.partial"


def promote(target: str, tmp: Path):
    """Moves a fully-downloaded temp file into the cache, then enforces the size cap."""
    if not tmp.is_file() or tmp.stat().st_size == 0:
        discard(tmp)
        return
    try:
        tmp.replace(cached_path(target))
    except OSError as e:
        logger.error("Failed to promote cached audio: %s", e)
        discard(tmp)
        return
    _evict_if_needed()


def discard(tmp: Path):
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _evict_if_needed():
    try:
        files = sorted(CACHE_DIR.glob("*.audio"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while total > MAX_CACHE_BYTES and files:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            logger.info("Evicted cached audio file to stay under the cache size cap: %s", oldest.name)
    except OSError as e:
        logger.error("Cache eviction failed: %s", e)
