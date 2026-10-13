import csv
from datetime import date, timedelta
from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import User

from .forms import StatisticsForm, WeightForm, WeightGoalForm
from .models import WeightEntry, WeightGoal
from .statistics import daily_records, progress_summary


def _period(form: StatisticsForm) -> tuple[date, date]:
    if form.cleaned_data.get("period") == "custom":
        return form.cleaned_data["start"], form.cleaned_data["end"]
    days = int(form.cleaned_data.get("period") or 30)
    end = timezone.localdate()
    return end - timedelta(days=days - 1), end


@login_required
@never_cache
@require_GET
def statistics_view(request: HttpRequest) -> HttpResponse:
    form = StatisticsForm(request.GET or {"period": "30"})
    if form.is_valid():
        start, end = _period(form)
    else:
        end = timezone.localdate()
        start = end - timedelta(days=29)
    context = progress_summary(cast(User, request.user), start, end)
    context.update({"filter_form": form, "export_query": request.GET.urlencode()})
    return render(request, "tracker/statistics.html", context)


@login_required
@never_cache
@require_GET
def statistics_export(request: HttpRequest) -> HttpResponse:
    form = StatisticsForm(request.GET or {"period": "30"})
    if not form.is_valid():
        return HttpResponse(
            gettext("Choose a valid date range."), status=400, content_type="text/plain"
        )
    start, end = _period(form)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="cotinga-{start.isoformat()}-{end.isoformat()}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(
        [
            "Date",
            "Calories (kcal)",
            "Protein (g)",
            "Carbs (g)",
            "Fat (g)",
            "Weight (kg)",
        ]
    )
    for record in daily_records(cast(User, request.user), start, end):
        writer.writerow(
            [
                record.date.isoformat(),
                record.calories,
                record.protein,
                record.carbs,
                record.fat,
                record.weight,
            ]
        )
    return response


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def weight_view(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    form = WeightForm(
        request.POST if request.method == "POST" else None,
        initial={"date": timezone.localdate()},
    )
    if request.method == "POST" and form.is_valid():
        WeightEntry.objects.update_or_create(
            user=user,
            date=form.cleaned_data["date"],
            defaults={
                "weight": form.cleaned_data["weight"],
                "notes": form.cleaned_data["notes"],
            },
        )
        messages.success(
            request,
            gettext(
                "Your weigh-in has been saved. An existing entry for that day is updated."
            ),
        )
        return redirect("tracker:weight")
    goal, _ = WeightGoal.objects.get_or_create(user=user)
    return render(
        request,
        "tracker/weight.html",
        {
            "form": form,
            "goal_form": WeightGoalForm(instance=goal),
            "entries": WeightEntry.objects.filter(user=user)[:60],
            "goal": goal,
        },
    )


@login_required
@require_POST
def weight_delete(request: HttpRequest, pk: int) -> HttpResponse:
    entry = get_object_or_404(WeightEntry, pk=pk, user=request.user)
    entry.delete()
    messages.success(request, gettext("Weigh-in removed."))
    return redirect("tracker:weight")


@login_required
@require_POST
def weight_goal(request: HttpRequest) -> HttpResponse:
    goal, _ = WeightGoal.objects.get_or_create(user=cast(User, request.user))
    form = WeightGoalForm(request.POST, instance=goal)
    if form.is_valid():
        form.save()
        messages.success(request, gettext("Your weight goal has been saved."))
        return redirect("tracker:weight")
    return render(
        request,
        "tracker/weight.html",
        {
            "form": WeightForm(initial={"date": timezone.localdate()}),
            "goal_form": form,
            "entries": WeightEntry.objects.filter(user=cast(User, request.user))[:60],
            "goal": goal,
        },
    )
