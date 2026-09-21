"""
JSON-file-backed persistence for saved queues and playback history.
Each Matrix room gets its own subdirectory keyed by a filesystem-safe slug of its room_id.
"""

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from bot.song_resolver import ResolvedSong

logger = logging.getLogger("radio.storage")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SAVED_QUEUES_DIR = DATA_DIR / "saved_queues"
HISTORY_DIR = DATA_DIR / "history"

HISTORY_LIMIT = 50

_UNSAFE_CHARS = re.compile(r'[^a-zA-Z0-9_.-]')


def _safe_slug(value: str) -> str:
    """Converts a room_id or queue name into a filesystem-safe slug."""
    return _UNSAFE_CHARS.sub("_", value.strip()) or "_"


def _room_dir(base: Path, room_id: str) -> Path:
    room_dir = base / _safe_slug(room_id)
    room_dir.mkdir(parents=True, exist_ok=True)
    return room_dir


def _song_to_dict(song: ResolvedSong) -> dict:
    return asdict(song)


def _song_from_dict(data: dict) -> ResolvedSong:
    return ResolvedSong(**data)


class QueueStore:
    """Persists named song-queue snapshots per room."""

    def save(self, room_id: str, name: str, songs: List[ResolvedSong], force: bool = False) -> bool:
        room_dir = _room_dir(SAVED_QUEUES_DIR, room_id)
        path = room_dir / f"{_safe_slug(name)}.json"
        if path.exists() and not force:
            return False

        payload = {"name": name, "songs": [_song_to_dict(s) for s in songs]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def load(self, room_id: str, name: str) -> Optional[List[ResolvedSong]]:
        path = _room_dir(SAVED_QUEUES_DIR, room_id) / f"{_safe_slug(name)}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [_song_from_dict(s) for s in payload.get("songs", [])]
        except Exception as e:
            logger.error("Failed to load saved queue '%s' for room %s: %s", name, room_id, e)
            return None

    def list(self, room_id: str) -> List[dict]:
        room_dir = _room_dir(SAVED_QUEUES_DIR, room_id)
        result = []
        for path in sorted(room_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result.append({"name": payload.get("name", path.stem), "count": len(payload.get("songs", []))})
            except Exception:
                continue
        return result

    def delete(self, room_id: str, name: str) -> bool:
        path = _room_dir(SAVED_QUEUES_DIR, room_id) / f"{_safe_slug(name)}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def rename(self, room_id: str, old_name: str, new_name: str) -> bool:
        room_dir = _room_dir(SAVED_QUEUES_DIR, room_id)
        old_path = room_dir / f"{_safe_slug(old_name)}.json"
        new_path = room_dir / f"{_safe_slug(new_name)}.json"
        if not old_path.exists() or new_path.exists():
            return False

        payload = json.loads(old_path.read_text(encoding="utf-8"))
        payload["name"] = new_name
        new_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        old_path.unlink()
        return True


class HistoryStore:
    """Persists a capped list of recently played songs per room."""

    def _path(self, room_id: str) -> Path:
        return HISTORY_DIR / f"{_safe_slug(room_id)}.json"

    def load(self, room_id: str) -> List[ResolvedSong]:
        path = self._path(room_id)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [_song_from_dict(s) for s in payload]
        except Exception as e:
            logger.error("Failed to load history for room %s: %s", room_id, e)
            return []

    def append(self, room_id: str, song: ResolvedSong) -> List[ResolvedSong]:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history = self.load(room_id)
        history.append(song)
        history = history[-HISTORY_LIMIT:]
        self._path(room_id).write_text(
            json.dumps([_song_to_dict(s) for s in history], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return history
