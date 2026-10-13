from datetime import date, timedelta
from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.utils import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import User

from .forms import MealForm, TargetForm
from .models import NUTRIENTS, DailyTarget, Meal
from .summary import summarize


def selected_date(request: HttpRequest) -> date:
    try:
        return date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return timezone.localdate()


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    day = selected_date(request)
    demo = not request.user.is_authenticated
    if demo:
        target = DailyTarget()
        meals = [
            Meal(
                kind="breakfast",
                description="Barley toast, honey, lentils, yogurt",
                calories=425,
                protein=25,
                carbs=55,
                fat=12,
            ),
            Meal(
                kind="lunch",
                description="Gheymeh, rice, doogh",
                calories=650,
                protein=35,
                carbs=80,
                fat=18,
            ),
        ]
    else:
        user = cast(User, request.user)
        target, _ = DailyTarget.objects.get_or_create(user=user)
        meals = list(Meal.objects.filter(user=user, date=day))
    totals = {n: sum(int(getattr(meal, n)) for meal in meals) for n in NUTRIENTS}
    return render(
        request,
        "tracker/dashboard.html",
        {
            "demo": demo,
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
    form = MealForm(
        request.POST if request.method == "POST" else None,
        initial={"date": selected_date(request)},
    )
    if request.method == "POST" and form.is_valid():
        meal = form.save(commit=False)
        meal.user = cast(User, request.user)
        meal.save()
        messages.success(request, "Meal added. Your daily summary is up to date.")
        return redirect(f"{reverse('tracker:dashboard')}?date={meal.date.isoformat()}")
    return render(
        request,
        "tracker/form.html",
        {
            "form": form,
            "title": "Log a meal",
            "description": "A quick note about what you ate. Estimates are welcome.",
            "submit_label": "Save meal",
        },
    )


@login_required
@require_POST
def meal_delete(request: HttpRequest, pk: int) -> HttpResponse:
    meal = get_object_or_404(Meal, pk=pk, user=request.user)
    day = meal.date
    meal.delete()
    messages.success(request, "Meal removed.")
    return redirect(f"{reverse('tracker:dashboard')}?date={day.isoformat()}")


@login_required
@require_http_methods(["GET", "POST"])
def targets_edit(request: HttpRequest) -> HttpResponse:
    target, _ = DailyTarget.objects.get_or_create(user=cast(User, request.user))
    form = TargetForm(
        request.POST if request.method == "POST" else None, instance=target
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your daily targets have been saved.")
        return redirect("tracker:dashboard")
    return render(
        request,
        "tracker/form.html",
        {
            "form": form,
            "title": "Your daily targets",
            "description": "Set the ranges that work for you. Starting values are examples from the demo.",
            "submit_label": "Save targets",
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
    return JsonResponse({"status": "ok"})
