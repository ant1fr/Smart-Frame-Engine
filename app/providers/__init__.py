"""Image source providers.

Every provider subclasses ``BaseImageProvider`` and returns a standardized
``ImageAsset`` (raw bytes, metadata, recommended fit mode, regions of interest).
Providers (Immich, TMDB media posters, direct URLs) are added in the
``feat/providers-api`` milestone.
"""
