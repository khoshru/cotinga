"""Shared, private body details; never guess missing personal measurements."""

from typing import Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User

from .forms import BodyProfileForm
from .models import BodyProfile, WeightEntry

PROFILE_FIELDS = ("age", "height", "sex", "activity", "goal", "notes")


def profile_initial(user: User) -> dict[str, Any]:
    profile = BodyProfile.objects.filter(user=user).first()
    latest = WeightEntry.objects.filter(
        user=user, date__lte=timezone.localdate()
    ).first()
    data = {name: getattr(profile, name) for name in PROFILE_FIELDS} if profile else {}
    data["weight"] = latest.weight if latest else None
    return data


def profile_context(
    user: User, return_to: str, *, auto_open: bool = True
) -> dict[str, Any]:
    profile = BodyProfile.objects.filter(user=user).first()
    initial = profile_initial(user)
    needed = profile is None or not profile.complete or initial["weight"] is None
    return {
        "body_profile": profile,
        "profile_needed": needed,
        "profile_auto_open": auto_open,
        "profile_return": return_to,
        "profile_form": BodyProfileForm(
            instance=profile, initial=initial, prefix="profile"
        ),
    }


@transaction.atomic
def save_profile(user: User, data: dict[str, Any]) -> BodyProfile:
    profile, _ = BodyProfile.objects.update_or_create(
        user=user,
        defaults={name: data[name] for name in PROFILE_FIELDS if name in data},
    )
    if data.get("weight") is not None:
        latest = WeightEntry.objects.filter(
            user=user, date__lte=timezone.localdate()
        ).first()
        if latest is None or latest.weight != data["weight"]:
            WeightEntry.objects.update_or_create(
                user=user,
                date=timezone.localdate(),
                defaults={"weight": data["weight"]},
            )
    return profile
