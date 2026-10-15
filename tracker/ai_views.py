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
    MealSaveForm,
    OpenRouterForm,
    TargetEstimateForm,
    TargetForm,
)
from .localization import localized_result
from .meal_imports import journal_state, submission_key
from .models import (
    DailyTarget,
    Meal,
    MealEstimate,
    OpenRouterSettings,
    TargetEstimate,
)
from .profiles import profile_context, profile_initial, save_profile
from .views import selected_date


class MealReviewForm(MealForm):
    class Meta(MealForm.Meta):
        fields = tuple(field for field in MealForm.Meta.fields if field != "date")


class MealReviewFormSet(forms.BaseFormSet):  # type: ignore[type-arg]
    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        kinds = [
            form.cleaned_data["kind"]
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if len(kinds) != len(set(kinds)):
            raise forms.ValidationError(
                gettext(
                    "Keep one entry per meal type. Combine the foods, or skip the extra entry."
                )
            )


ReviewFormSet = forms.formset_factory(
    MealReviewForm,
    formset=MealReviewFormSet,
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


def _create_estimate(
    user: User, data: dict[str, Any], model: str
) -> tuple[MealEstimate, bool]:
    key = submission_key(data)
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        existing = MealEstimate.objects.filter(user=user, submission_key=key).first()
        if existing is not None:
            if existing.status != MealEstimate.Status.FAILED and (
                existing.status != MealEstimate.Status.PROCESSING
                or existing.created_at > timezone.now() - timedelta(minutes=2)
            ):
                return existing, False
            existing.submission_key = ""
            existing.status = MealEstimate.Status.FAILED
            existing.save(update_fields=["submission_key", "status"])
        _check_estimate_limits(user)
        return MealEstimate.objects.create(
            user=user, date=data["date"], model=model, submission_key=key
        ), True


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
    form = TargetEstimateForm(
        request.POST if request.method == "POST" else None,
        initial=profile_initial(user),
    )
    if request.method == "POST" and form.is_valid():
        save_profile(user, form.cleaned_data)
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
    context = profile_context(user, reverse("tracker:ai-targets"))
    if form.is_bound:
        context["profile_needed"] = False
    context.update({"form": form, "preferences": preferences})
    return render(
        request,
        "tracker/target_estimate.html",
        context,
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
            batch, created = _create_estimate(user, form.cleaned_data, model.id)
            if not created:
                if batch.status == MealEstimate.Status.SAVED:
                    messages.info(
                        request, gettext("These meals are already in your journal.")
                    )
                    return redirect(
                        f"{reverse('tracker:dashboard')}?date={batch.date.isoformat()}"
                    )
                if batch.status == MealEstimate.Status.READY:
                    return redirect("tracker:ai-review", pk=batch.pk)
                form.add_error(
                    None,
                    gettext(
                        "This estimate is already processing. Please wait a moment before opening it again."
                    ),
                )
                return render(
                    request,
                    "tracker/estimate.html",
                    {"form": form, "preferences": preferences},
                )
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
    user = cast(User, request.user)
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
    with transaction.atomic():
        if request.method == "POST":
            User.objects.select_for_update().only("pk").get(pk=user.pk)
        existing = list(Meal.objects.filter(user=user, date=batch.date))
        state = journal_state(existing)
        valid = formset.is_valid() if request.method == "POST" else False
        selected_kinds = (
            {
                str(form.cleaned_data["kind"])
                for form in formset
                if valid and not form.cleaned_data.get("DELETE")
            }
            if request.method == "POST"
            else {str(row["kind"]) for row in batch.result["meals"]}
        )
        replacements = [meal for meal in existing if meal.kind in selected_kinds]
        save_data = request.POST.copy() if request.method == "POST" else None
        stale = (
            request.method == "POST" and request.POST.get("journal_state", "") != state
        )
        if stale:
            refreshed = request.POST.copy()
            refreshed["journal_state"] = state
            refreshed.pop("confirm_replacement", None)
            save_data = refreshed
        save_form = MealSaveForm(save_data, initial={"journal_state": state})
        save_form.fields["confirm_replacement"].required = bool(replacements)
        save_valid = save_form.is_valid() if request.method == "POST" else False
        if stale:
            save_form.add_error(
                None,
                gettext(
                    "This day changed while you were reviewing. Check the current entries below and confirm again."
                ),
            )
        already_saved = MealEstimate.objects.filter(
            pk=batch.pk, status=MealEstimate.Status.SAVED
        ).exists()
        if request.method == "POST" and (
            already_saved or (valid and save_valid and not stale)
        ):
            claimed = MealEstimate.objects.filter(
                pk=batch.pk,
                user=cast(User, request.user),
                status=MealEstimate.Status.READY,
            ).update(status=MealEstimate.Status.SAVED, saved_at=timezone.now())
            if claimed:
                # Keep the old values with the saved estimate for recovery. Only
                # the explicitly reviewed meal types are replaced, on this day.
                previous = list(
                    Meal.objects.filter(
                        pk__in=[meal.pk for meal in replacements]
                    ).values(
                        "id",
                        "date",
                        "kind",
                        "time",
                        "description",
                        "description_fa",
                        "portion",
                        "portion_fa",
                        "calories",
                        "protein",
                        "carbs",
                        "fat",
                        "estimated",
                        "estimate_model",
                    )
                )
                batch.result["replaced_meals"] = [
                    {
                        key: value.isoformat() if hasattr(value, "isoformat") else value
                        for key, value in row.items()
                    }
                    for row in previous
                ]
                batch.save(update_fields=["result"])
                Meal.objects.filter(pk__in=[meal.pk for meal in replacements]).delete()
                meals = []
                for form in formset:
                    if not form.cleaned_data.get("DELETE"):
                        meal = form.save(commit=False)
                        meal.user = cast(User, request.user)
                        meal.date = batch.date
                        meal.estimated = True
                        meal.estimate_model = batch.model
                        meals.append(meal)
                Meal.objects.bulk_create(meals)
            messages.success(
                request,
                gettext("Your reviewed meals have been saved for this day.")
                if claimed
                else gettext("These meals are already in your journal."),
            )
            return redirect(
                f"{reverse('tracker:dashboard')}?date={batch.date.isoformat()}"
            )
    proposed = (
        sum(
            int(form.cleaned_data.get("calories", 0))
            for form in formset
            if valid and not form.cleaned_data.get("DELETE")
        )
        if request.method == "POST"
        else sum(int(row["calories"]) for row in batch.result["meals"])
    )
    current_total = sum(meal.calories for meal in existing)
    replacement_total = sum(meal.calories for meal in replacements)
    return render(
        request,
        "tracker/review.html",
        {
            "batch": batch,
            "formset": formset,
            "result": localized_result(batch.result),
            "save_form": save_form,
            "existing_meals": existing,
            "existing_kinds": ",".join(sorted({meal.kind for meal in existing})),
            "replacements": replacements,
            "current_total": current_total,
            "proposed_total": proposed,
            "updated_total": current_total - replacement_total + proposed,
        },
    )
