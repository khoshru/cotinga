"""Reviewed movement context shared by the library and equipment-constrained AI."""

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from .models import Exercise

FOCUS_LABELS = {
    "upper_back": _("Upper & middle back"),
    "shoulder_blades": _("Shoulder blade control"),
    "lower_back": _("Lower back"),
    "spine_mobility": _("Spine mobility"),
    "back_mobility": _("Back mobility"),
    "posterior_chain": _("Hips & hamstrings"),
    "hips": _("Hips & glutes"),
    "legs": _("Legs"),
    "arms": _("Arms"),
    "shoulders": _("Shoulders"),
    "chest": _("Chest"),
    "calves": _("Calves"),
    "core": _("Core"),
    "full_body": _("Full body"),
    "balance": _("Balance"),
    "back": _("Back"),
}
KIND_LABELS = {
    "cardio": _("Cardio"),
    "strength": _("Strength"),
    "mobility": _("Mobility"),
    "balance": _("Balance"),
}
FOCUS_SEARCH = {
    "upper_back": "upper middle mid back upper-middle rhomboids trapezius پشت میانی بالای کتف ზედა შუა ზურგი ბეჭები",
    "shoulder_blades": "shoulder blade scapular serratus کتف شانه ბეჭი მხარი",
    "lower_back": "lower back lumbar پایین کمر წელი ქვედა ზურგი",
    "spine_mobility": "spine mobility back حرکت ستون فقرات მოძრაობა ხერხემალი",
    "back_mobility": "back mobility stretch پشت کشش ზურგი გაჭიმვა",
}
FIXTURE_LABELS = {
    "bench": _("Exercise bench or sturdy seat"),
    "pullup_bar": _("Pull-up bar"),
    "dip_bars": _("Dip bars"),
    "box": _("Stable exercise step"),
    "rack": _("Squat rack"),
    "band_anchor": _("Secure resistance-band anchor"),
    "ab_wheel": _("Ab wheel"),
    "foot_anchor": _("Nordic curl foot anchor"),
    "back_extension_bench": _("Back extension bench"),
    "jump_rope": _("Jump rope"),
    "battle_ropes": _("Battle ropes and secure anchor"),
}


@lru_cache(maxsize=1)
def movement_details() -> dict[str, Any]:
    with Path(__file__).with_name("exercise_details.json").open(encoding="utf-8") as f:
        return dict(json.load(f))


def matching_movements(term: str) -> list[str]:
    return [
        slug
        for slug, item in movement_details().items()
        if term.casefold()
        in (
            " ".join(
                str(item.get(key, ""))
                for key in ("focus", "kind", "cue", "cue_fa", "cue_ka")
            )
            + " "
            + FOCUS_SEARCH.get(item.get("focus", ""), "")
        )
        .replace("_", " ")
        .casefold()
    ]


def equipment_requirements(exercise: Exercise) -> set[str]:
    """Fixtures are never silently assumed from primary equipment availability."""
    if exercise.owner_id or exercise.equipment == "machine":
        return set()
    slug = exercise.slug or ""
    details = movement_details().get(slug, {})
    if "requires" in details:
        return set(details["requires"])
    needed: set[str] = set()
    if any(
        part in slug
        for part in (
            "bench",
            "incline",
            "decline",
            "chest-supported",
            "bulgarian",
            "seated-shoulder",
            "seated-calf",
            "preacher",
            "hip-thrust",
        )
    ):
        needed.add("bench")
    if slug in {
        "pull-up",
        "chin-up",
        "neutral-grip-pull-up",
        "dead-hang",
        "inverted-row",
        "hanging-leg-raise",
        "hanging-knee-raise",
    }:
        needed.add("pullup_bar")
    if slug in {"dip", "chest-dip"}:
        needed.add("dip_bars")
    if "step-up" in slug or slug == "box-jump":
        needed.add("box")
    if exercise.equipment == "barbell" and (
        "bench-press" in slug
        or slug
        in {
            "barbell-back-squat",
            "barbell-front-squat",
            "barbell-rack-pull",
            "barbell-floor-press",
        }
    ):
        needed.add("rack")
    if slug == "ab-wheel-rollout":
        needed.add("ab_wheel")
    if slug == "nordic-curl":
        needed.add("foot_anchor")
    if slug == "back-extension":
        needed.add("back_extension_bench")
    return needed
