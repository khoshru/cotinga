"""Identify replayed submissions and changes to the day being reviewed."""

import hashlib
import json
import uuid
from copy import copy
from typing import Any, cast

from django.core.validators import MaxLengthValidator
from django.db.models import Field
from django.utils.crypto import salted_hmac

from .models import NUTRIENTS, Meal


def combine_meals(existing: list[Meal], addition: Meal) -> Meal:
    """Keep one entry, with every food and each contribution counted once."""
    combined = copy(existing[0])
    parts = [*existing, addition]
    for field in ("description", "portion"):
        setattr(
            combined,
            field,
            "; ".join(getattr(part, field) for part in parts if getattr(part, field)),
        )
        for language in ("fa", "ka"):
            translated = f"{field}_{language}"
            # Fall back per contribution so a missing translation never hides food.
            text = "; ".join(
                getattr(part, translated) or getattr(part, field)
                for part in parts
                if getattr(part, field)
            )
            setattr(
                combined,
                translated,
                text if any(getattr(part, translated) for part in parts) else "",
            )
    for field in NUTRIENTS:
        setattr(combined, field, sum(getattr(part, field) for part in parts))
    times = [part.time for part in parts if part.time is not None]
    combined.time = min(times) if times else None
    for name in (
        "description",
        "description_fa",
        "description_ka",
        "portion",
        "portion_fa",
        "portion_ka",
        *NUTRIENTS,
    ):
        field_object = cast("Field[Any, Any]", Meal._meta.get_field(name))
        field_object.clean(getattr(combined, name), combined)
        if field_object.max_length is not None:
            MaxLengthValidator(field_object.max_length)(getattr(combined, name))
    return combined


def submission_key(data: dict[str, Any]) -> str:
    # A new form can log the same food intentionally. Back/refresh reuses the
    # original form's UUID, but changed food or dates remain separate requests.
    value = json.dumps(
        [
            str(data.get("submission_id") or uuid.uuid4()),
            data["date"].isoformat(),
            data["prompt"],
        ]
    )
    return salted_hmac("tracker.meal_submission", value, algorithm="sha256").hexdigest()


def journal_state(meals: list[Meal]) -> str:
    if not meals:
        return ""
    fields = (
        "pk",
        "kind",
        "date",
        "time",
        "description",
        "description_fa",
        "description_ka",
        "portion",
        "portion_fa",
        "portion_ka",
        "estimate_id",
        *NUTRIENTS,
    )
    value = json.dumps(
        [
            [
                *[str(getattr(meal, field)) for field in fields],
                sorted(str(source.pk) for source in meal.source_estimates.all()),
            ]
            for meal in meals
        ]
    )
    return hashlib.sha256(value.encode()).hexdigest()
