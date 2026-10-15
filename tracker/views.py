import os
from datetime import date, timedelta
from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.db.utils import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import User

from .forms import MealForm, TargetForm
from .models import NUTRIENTS, DailyTarget, Meal, MealEstimate
from .summary import summarize


def selected_date(request: HttpRequest) -> date:
    try:
        return date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return timezone.localdate()


@login_required
@require_GET
def original_input(request: HttpRequest, pk: str) -> HttpResponse:
    batch = get_object_or_404(MealEstimate, pk=pk, user=request.user)
    response = render(request, "tracker/original_input.html", {"batch": batch})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    day = selected_date(request)
    user = cast(User, request.user)
    target, _ = DailyTarget.objects.get_or_create(user=user)
    meals = list(Meal.objects.filter(user=user, date=day))
    totals = {n: sum(int(getattr(meal, n)) for meal in meals) for n in NUTRIENTS}
    return render(
        request,
        "tracker/dashboard.html",
        {
            "day": day,
            "today": timezone.localdate(),
            "previous_day": day - timedelta(days=1) if day > date.min else day,
            "next_day": day + timedelta(days=1) if day < date.max else day,
            "meals": meals,
            "nutrients": summarize(totals, target),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def meal_create(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    form = MealForm(
        request.POST if request.method == "POST" else None,
        initial={"date": selected_date(request)},
        instance=Meal(user=user),
    )
    if request.method == "POST":
        with transaction.atomic():
            User.objects.select_for_update().only("pk").get(pk=user.pk)
            if form.is_valid():
                meal = form.save()
                messages.success(
                    request, gettext("Meal added. Your daily summary is up to date.")
                )
                return redirect(
                    f"{reverse('tracker:dashboard')}?date={meal.date.isoformat()}"
                )
    return render(
        request,
        "tracker/form.html",
        {
            "form": form,
            "title": gettext("Log a meal"),
            "description": gettext(
                "A quick note about what you ate. Estimates are welcome."
            ),
            "submit_label": gettext("Save meal"),
        },
    )


@login_required
@require_POST
def meal_delete(request: HttpRequest, pk: int) -> HttpResponse:
    meal = get_object_or_404(Meal, pk=pk, user=request.user)
    day = meal.date
    meal.delete()
    messages.success(request, gettext("Meal removed."))
    return redirect(f"{reverse('tracker:dashboard')}?date={day.isoformat()}")


@login_required
@require_http_methods(["GET", "POST"])
def meal_edit(request: HttpRequest, pk: int) -> HttpResponse:
    user = cast(User, request.user)
    meal = get_object_or_404(Meal, pk=pk, user=request.user)
    form = MealForm(request.POST if request.method == "POST" else None, instance=meal)
    if request.method == "POST":
        with transaction.atomic():
            User.objects.select_for_update().only("pk").get(pk=user.pk)
            if form.is_valid():
                form.save()
                messages.success(request, gettext("Meal updated."))
                return redirect(
                    f"{reverse('tracker:dashboard')}?date={meal.date.isoformat()}"
                )
    return render(
        request,
        "tracker/form.html",
        {
            "form": form,
            "title": gettext("Edit meal"),
            "description": gettext(
                "Adjust the food, portion, time, or nutrition values for this entry."
            ),
            "submit_label": gettext("Save changes"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def targets_edit(request: HttpRequest) -> HttpResponse:
    target, _ = DailyTarget.objects.get_or_create(user=cast(User, request.user))
    form = TargetForm(
        request.POST if request.method == "POST" else None, instance=target
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, gettext("Your daily targets have been saved."))
        return redirect("tracker:dashboard")
    return render(
        request,
        "tracker/form.html",
        {
            "form": form,
            "title": gettext("Your daily targets"),
            "description": gettext(
                "Set the daily calorie and nutrient ranges that work for you."
            ),
            "submit_label": gettext("Save targets"),
            "targets_form": True,
        },
    )


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return JsonResponse({"status": "unavailable"}, status=503)
    result = {"status": "ok"}
    if revision := os.environ.get("COTINGA_REVISION", ""):
        result["revision"] = revision
    return JsonResponse(result)
