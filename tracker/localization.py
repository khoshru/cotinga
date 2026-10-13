"""English source text with optional Farsi translations, never duplicate nutrition."""

import unicodedata
from typing import Any

from django.utils.translation import get_language

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def is_farsi() -> bool:
    return (get_language() or "en").startswith("fa")


def english_text(value: str) -> str:
    """Normalize digits and reject untranslated source text without guessing."""
    value = value.translate(_DIGITS).strip()
    if any(
        character.isalpha() and "LATIN" not in unicodedata.name(character, "")
        for character in value
    ):
        raise ValueError("Source text must use English or transliterated food names.")
    return value


def localized_text(english: str, farsi: str) -> str:
    return farsi if is_farsi() and farsi else english


def localized_result(result: dict[str, Any]) -> dict[str, Any]:
    displayed = result.copy()
    if is_farsi():
        for field in ("explanation", "assumptions"):
            if result.get(f"{field}_fa"):
                displayed[field] = result[f"{field}_fa"]
    return displayed
