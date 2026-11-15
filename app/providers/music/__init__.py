"""Music now-playing support: album cover lookup (Deezer, MusicBrainz / Cover Art Archive)."""

from app.providers.music.artwork import (
    Artwork,
    ArtworkMatch,
    ArtworkResolver,
    MinIntervalLimiter,
)

__all__ = ["Artwork", "ArtworkMatch", "ArtworkResolver", "MinIntervalLimiter"]
