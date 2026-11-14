"""Image source providers.

Every provider subclasses ``BaseImageProvider`` and returns a standardized
``ImageAsset`` (raw bytes, metadata, recommended fit mode, regions of interest).
Implemented: ``immich`` (multi-account, face-aware). TMDB posters and direct URLs follow.
"""
