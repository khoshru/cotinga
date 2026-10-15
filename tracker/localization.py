"""Canonical English with optional translations, never duplicate nutrition."""

import unicodedata
from typing import Any

from django.utils.translation import get_language

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def is_farsi() -> bool:
    return (get_language() or "en").startswith("fa")


def translation_language() -> str:
    language = (get_language() or "en").split("-")[0]
    return language if language in ("fa", "ka") else ""


def english_text(value: str) -> str:
    """Normalize digits and reject untranslated source text without guessing."""
    value = value.translate(_DIGITS).strip()
    if any(
        character.isalpha() and "LATIN" not in unicodedata.name(character, "")
        for character in value
    ):
        raise ValueError("Source text must use English or transliterated food names.")
    return value


def localized_text(english: str, farsi: str = "", georgian: str = "") -> str:
    return {"fa": farsi, "ka": georgian}.get(translation_language()) or english


def localized_result(result: dict[str, Any]) -> dict[str, Any]:
    displayed = result.copy()
    language = translation_language()
    if language:
        for field in ("explanation", "assumptions"):
            if result.get(f"{field}_{language}"):
                displayed[field] = result[f"{field}_{language}"]
    return displayed
