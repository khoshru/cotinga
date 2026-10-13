from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods

from accounts.models import User

from . import openrouter
from .credentials import CredentialError, decrypt_key, encrypt_key
from .forms import (
    EstimateForm,
    MealForm,
    OpenRouterForm,
    TargetEstimateForm,
    TargetForm,
)
from .localization import localized_result
from .models import (
    DailyTarget,
    Meal,
    MealEstimate,
    OpenRouterSettings,
    TargetEstimate,
    WeightEntry,
)
from .views import selected_date


class MealReviewForm(MealForm):
    class Meta(MealForm.Meta):
        fields = tuple(field for field in MealForm.Meta.fields if field != "date")


ReviewFormSet = forms.formset_factory(
    MealReviewForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
    max_num=openrouter.MAX_MEALS,
    validate_max=True,
    absolute_max=openrouter.MAX_MEALS,
)


@login_required
@never_cache
@sensitive_post_parameters("api_key")
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def settings_view(request: HttpRequest) -> HttpResponse:
    settings, _ = OpenRouterSettings.objects.get_or_create(
        user=cast(User, request.user)
    )
    form = OpenRouterForm(
        request.POST if request.method == "POST" else None,
        initial={"model": settings.model},
    )
    available: list[openrouter.ModelInfo] = []
    catalog_error = ""
    try:
        available = openrouter.models()
    except openrouter.OpenRouterError as exc:
        catalog_error = str(exc)
    if request.method == "POST" and form.is_valid():
        try:
            model = str(form.cleaned_data.get("model", ""))
            if model and not (catalog_error and model == settings.model):
                openrouter.find_model(model)
            key = str(form.cleaned_data.get("api_key", ""))
            if key:
                openrouter.validate_key(key)
                settings.encrypted_api_key = encrypt_key(key)
            elif form.cleaned_data.get("remove_key"):
                settings.encrypted_api_key = ""
            settings.model = model
            settings.save()
            messages.success(request, gettext("Your AI settings have been saved."))
            return redirect("tracker:ai-settings")
        except openrouter.OpenRouterError as exc:
            form.add_error(None, str(exc))
    query = request.GET.get("q", "")[:100].strip().lower()
    choices = [
        model
        for model in available
        if not query or query in f"{model.name} {model.id}".lower()
    ]
    current = str(form["model"].value() or "")
    response = render(
        request,
        "tracker/ai_settings.html",
        {
            "form": form,
            "preferences": settings,
            "models": choices,
            "current_model": current,
            "selected_model": next(
                (model for model in available if model.id == current), None
            ),
            "catalog_error": catalog_error,
            "query": query,
            "current_missing": bool(
                current and not any(model.id == current for model in choices)
            ),
        },
    )
    return response


@login_required
@never_cache
@require_GET
def model_search(request: HttpRequest) -> JsonResponse:
    try:
        query = request.GET.get("q", "")[:100].strip().lower()
        free_only = request.GET.get("free") == "1"
        choices = [
            model
            for model in openrouter.models()
            if (not query or query in f"{model.name} {model.id}".lower())
            and (not free_only or model.free)
        ]
        return JsonResponse(
            {
                "models": [
                    {"id": model.id, "name": model.name, "pricing": model.price_label}
                    for model in choices[:60]
                ],
                "total": len(choices),
            }
        )
    except openrouter.OpenRouterError as exc:
        return JsonResponse({"error": str(exc)}, status=503)


def _check_estimate_limits(user: User) -> None:
    now = timezone.now()
    recent = [
        MealEstimate.objects.filter(user=user),
        TargetEstimate.objects.filter(user=user),
    ]
    if any(
        query.filter(created_at__gte=now - timedelta(seconds=20)).exists()
        for query in recent
    ):
        raise openrouter.OpenRouterError(
            gettext("Please wait 20 seconds before making another estimate.")
        )
    if (
        sum(
            query.filter(created_at__date=timezone.localdate()).count()
            for query in recent
        )
        >= 50
    ):
        raise openrouter.OpenRouterError(
            gettext(
                "You have reached today's 50-estimate limit. You can still log meals and edit targets manually."
            )
        )


def _create_estimate(user: User, data: dict[str, Any], model: str) -> MealEstimate:
    with transaction.atomic():
        _check_estimate_limits(user)
        return MealEstimate.objects.create(user=user, date=data["date"], model=model)


@login_required
@never_cache
@sensitive_post_parameters("age", "height", "weight", "sex", "notes")
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def targets_estimate_view(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    preferences = OpenRouterSettings.objects.filter(user=user).first()
    if preferences is None or not preferences.has_key or not preferences.model:
        messages.info(
            request,
            gettext("Connect OpenRouter in AI settings to get target suggestions."),
        )
        return redirect("tracker:ai-settings")
    latest = WeightEntry.objects.filter(user=user).first()
    form = TargetEstimateForm(
        request.POST if request.method == "POST" else None,
        initial={
            "weight": latest.weight if latest else None,
            "goal": "lose",
            "activity": "light",
        },
    )
    if request.method == "POST" and form.is_valid():
        batch = None
        try:
            key = decrypt_key(preferences.encrypted_api_key)
            model = openrouter.find_model(preferences.model)
            with transaction.atomic():
                _check_estimate_limits(user)
                batch = TargetEstimate.objects.create(user=user, model=model.id)
            result = openrouter.estimate_targets(key, model, form.cleaned_data)
            batch.result = result
            batch.status = MealEstimate.Status.READY
            batch.save(update_fields=["result", "status"])
            return redirect("tracker:ai-targets-review", pk=batch.pk)
        except (openrouter.OpenRouterError, CredentialError) as exc:
            if batch is not None:
                batch.status = MealEstimate.Status.FAILED
                batch.save(update_fields=["status"])
            form.add_error(None, str(exc))
    return render(
        request,
        "tracker/target_estimate.html",
        {"form": form, "preferences": preferences},
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def targets_review_view(request: HttpRequest, pk: UUID) -> HttpResponse:
    batch = get_object_or_404(TargetEstimate, pk=pk, user=request.user)
    if batch.status == MealEstimate.Status.SAVED:
        messages.info(request, gettext("These targets have already been applied."))
        return redirect("tracker:targets-edit")
    if batch.status != MealEstimate.Status.READY:
        return redirect("tracker:ai-targets")
    form = TargetForm(
        request.POST if request.method == "POST" else None,
        initial=batch.result["targets"],
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            claimed = TargetEstimate.objects.filter(
                pk=batch.pk,
                user=cast(User, request.user),
                status=MealEstimate.Status.READY,
            ).update(status=MealEstimate.Status.SAVED, saved_at=timezone.now())
            if claimed:
                DailyTarget.objects.update_or_create(
                    user=cast(User, request.user),
                    defaults={
                        field: form.cleaned_data[field]
                        for field in openrouter.TARGET_FIELDS
                    },
                )
        messages.success(
            request, gettext("Your reviewed daily targets have been applied.")
        )
        return redirect("tracker:dashboard")
    return render(
        request,
        "tracker/target_review.html",
        {"form": form, "batch": batch, "result": localized_result(batch.result)},
    )


@login_required
@never_cache
@sensitive_post_parameters("prompt")
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def estimate_view(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    preferences = OpenRouterSettings.objects.filter(user=user).first()
    if preferences is None or not preferences.has_key or not preferences.model:
        messages.info(
            request,
            gettext("Save your OpenRouter key and choose a model to estimate meals."),
        )
        return redirect("tracker:ai-settings")
    form = EstimateForm(
        request.POST if request.method == "POST" else None,
        initial={"date": selected_date(request)},
    )
    if request.method == "POST" and form.is_valid():
        batch = None
        try:
            key = decrypt_key(preferences.encrypted_api_key)
            model = openrouter.find_model(preferences.model)
            batch = _create_estimate(user, form.cleaned_data, model.id)
            result = openrouter.estimate_day(
                key, model, form.cleaned_data["prompt"], batch.date
            )
            batch.result = result
            batch.status = MealEstimate.Status.READY
            batch.save(update_fields=["result", "status"])
            return redirect("tracker:ai-review", pk=batch.pk)
        except (openrouter.OpenRouterError, CredentialError) as exc:
            if batch is not None:
                batch.status = MealEstimate.Status.FAILED
                batch.save(update_fields=["status"])
            form.add_error(None, str(exc))
    return render(
        request, "tracker/estimate.html", {"form": form, "preferences": preferences}
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def review_view(request: HttpRequest, pk: UUID) -> HttpResponse:
    batch = get_object_or_404(MealEstimate, pk=pk, user=request.user)
    if batch.status == MealEstimate.Status.SAVED:
        messages.info(request, gettext("These meals are already in your journal."))
        return redirect(f"{reverse('tracker:dashboard')}?date={batch.date.isoformat()}")
    if batch.status != MealEstimate.Status.READY:
        messages.info(
            request,
            gettext(
                "This estimate is not ready. Please try estimating your meals again."
            ),
        )
        return redirect("tracker:ai-estimate")
    formset = ReviewFormSet(
        request.POST if request.method == "POST" else None,
        initial=batch.result["meals"],
        prefix="meals",
    )
    if request.method == "POST" and formset.is_valid():
        with transaction.atomic():
            claimed = MealEstimate.objects.filter(
                pk=batch.pk,
                user=cast(User, request.user),
                status=MealEstimate.Status.READY,
            ).update(status=MealEstimate.Status.SAVED, saved_at=timezone.now())
            if claimed:
                meals = []
                for form in formset:
                    if not form.cleaned_data.get("DELETE"):
                        meal = form.save(commit=False)
                        meal.user = cast(User, request.user)
                        meal.date = batch.date
                        meal.estimated = True
                        meal.estimate_model = batch.model
                        meals.append(meal)
                # Form validation bounds every value; the transaction saves the whole day once.
                Meal.objects.bulk_create(meals)
        messages.success(
            request,
            gettext("The reviewed meals were added to your day.")
            if claimed
            else gettext("These meals are already in your journal."),
        )
        return redirect(f"{reverse('tracker:dashboard')}?date={batch.date.isoformat()}")
    return render(
        request,
        "tracker/review.html",
        {"batch": batch, "formset": formset, "result": localized_result(batch.result)},
    )
