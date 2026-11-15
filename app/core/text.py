"""Text normalization shared by providers (title and artist matching)."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_NON_WORD: Final = re.compile(r"[^\w]+")
#: Letters that Unicode decomposition does not split into base letters.
_TRANSLITERATIONS: Final = str.maketrans(
    {
        "æ": "ae",
        "œ": "oe",
        "ø": "o",
        "đ": "d",
        "ð": "d",
        "ł": "l",
        "þ": "th",
        "\u0131": "i",  # dotless i
    }
)


def fold(text: str) -> str:
    """Accent-, case- and punctuation-insensitive form of a title or name.

    ``"Le Fabuleux Destin d'Amélie"`` and ``"le fabuleux destin d amelie"`` fold to the same
    value, so do ``"AC/DC"`` and ``"ac dc"``, or ``"Ágætis byrjun"`` and ``"agaetis byrjun"``.
    """
    lowered = text.casefold().translate(_TRANSLITERATIONS)
    decomposed = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_NON_WORD.sub(" ", stripped).split())
