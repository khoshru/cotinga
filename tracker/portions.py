from decimal import Decimal
from typing import Any, cast

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from accounts.models import User

from .models import Meal, SavedPortion, food_name_key

MAX_SAVED_PORTIONS = 100


class SavedPortionForm(forms.Form):
    name = forms.CharField(max_length=100)
    quantity = forms.DecimalField(
        min_value=Decimal("0.01"), max_value=100000, max_digits=8, decimal_places=2
    )
    unit = forms.ChoiceField(choices=SavedPortion.Unit.choices)
    note = forms.CharField(max_length=140, required=False)

    def clean_name(self) -> str:
        return " ".join(str(self.cleaned_data["name"]).split())

    def clean_note(self) -> str:
        return " ".join(str(self.cleaned_data["note"]).split())


def serialize_portion(portion: SavedPortion) -> dict[str, Any]:
    return {
        "id": portion.pk,
        "name": portion.name,
        "quantity": format(portion.quantity, ".2f").rstrip("0").rstrip("."),
        "unit": portion.unit,
        "unit_label": str(portion.get_unit_display()),
        "note": portion.note,
        "url": reverse("tracker:portion-action", args=[portion.pk]),
    }


def portion_context(user: User, kind: str) -> dict[str, Any]:
    return {
        "saved_portions": [
            serialize_portion(portion)
            for portion in SavedPortion.objects.filter(user=user, is_active=True)
        ],
        "portion_units": SavedPortion.Unit.choices,
        "portion_kinds": Meal.Kind.choices,
        "portion_kind": kind if kind in Meal.Kind.values else "",
    }


@login_required
@never_cache
@sensitive_post_parameters("name", "note")
@sensitive_variables()
@require_POST
def save_portion(request: HttpRequest) -> JsonResponse:
    form = SavedPortionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    user = cast(User, request.user)
    key = food_name_key(form.cleaned_data["name"])
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        existing = SavedPortion.objects.filter(user=user, normalized_name=key).first()
        if (existing is None or not existing.is_active) and SavedPortion.objects.filter(
            user=user, is_active=True
        ).count() >= MAX_SAVED_PORTIONS:
            return JsonResponse(
                {
                    "error": gettext_lazy(
                        "You have 100 saved foods. Forget one before saving another."
                    )
                },
                status=400,
            )
        portion, _ = SavedPortion.objects.update_or_create(
            user=user,
            normalized_name=key,
            defaults={
                **form.cleaned_data,
                "is_active": True,
                "last_used_at": timezone.now(),
            },
        )
    return JsonResponse({"portion": serialize_portion(portion)})


@login_required
@never_cache
@sensitive_variables()
@require_POST
def portion_action(request: HttpRequest, pk: int) -> JsonResponse:
    if pk >= 2**63:
        raise Http404
    user = cast(User, request.user)
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        portion = get_object_or_404(SavedPortion, pk=pk, user=user)
        action = request.POST.get("action")
        if action == "restore":
            if (
                not portion.is_active
                and SavedPortion.objects.filter(user=user, is_active=True).count()
                >= MAX_SAVED_PORTIONS
            ):
                return JsonResponse(
                    {
                        "error": gettext(
                            "Your saved foods are full. Forget another food first."
                        )
                    },
                    status=400,
                )
            portion.is_active = True
        elif action == "archive":
            portion.is_active = False
        elif action == "use" and portion.is_active:
            portion.last_used_at = timezone.now()
        else:
            return JsonResponse(
                {"error": gettext("Choose a valid saved food action.")}, status=400
            )
        portion.save(update_fields=["is_active", "last_used_at", "updated_at"])
    return JsonResponse({"portion": serialize_portion(portion)})
