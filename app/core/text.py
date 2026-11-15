"""Text normalization shared by providers (title and artist matching)."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_NON_WORD: Final = re.compile(r"[^\w]+")


def fold(text: str) -> str:
    """Accent-, case- and punctuation-insensitive form of a title or name.

    ``"Le Fabuleux Destin d'Amélie"`` and ``"le fabuleux destin d amelie"`` fold to the same
    value, so do ``"AC/DC"`` and ``"ac dc"``.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_NON_WORD.sub(" ", stripped.casefold()).split())
