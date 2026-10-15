"""Identify replayed submissions and changes to the day being reviewed."""

import hashlib
import json
import uuid
from typing import Any

from django.utils.crypto import salted_hmac

from .models import NUTRIENTS, Meal


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
        *NUTRIENTS,
    )
    value = json.dumps(
        [[str(getattr(meal, field)) for field in fields] for meal in meals]
    )
    return hashlib.sha256(value.encode()).hexdigest()
