"""
Per-room audio settings (volume, normalization, fade-in) and FFmpeg filter builder.
"""

from dataclasses import dataclass
from typing import List

DEFAULT_VOLUME_PERCENT = 100
DEFAULT_NORMALIZE = False
DEFAULT_FADEIN_MS = 0

MIN_VOLUME_PERCENT = 0
MAX_VOLUME_PERCENT = 200
MAX_FADEIN_MS = 5000


@dataclass
class AudioSettings:
    volume_percent: int = DEFAULT_VOLUME_PERCENT
    normalize: bool = DEFAULT_NORMALIZE
    fadein_ms: int = DEFAULT_FADEIN_MS

    def build_af_args(self) -> List[str]:
        """Builds FFmpeg '-af' filter chain arguments from the current settings."""
        filters = []

        if self.volume_percent != DEFAULT_VOLUME_PERCENT:
            filters.append(f"volume={self.volume_percent / 100:.2f}")

        if self.normalize:
            filters.append("loudnorm")

        if self.fadein_ms > 0:
            filters.append(f"afade=t=in:st=0:d={self.fadein_ms / 1000:.2f}")

        if not filters:
            return []
        return ["-af", ",".join(filters)]
