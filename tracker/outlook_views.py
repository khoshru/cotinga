from decimal import Decimal
from typing import Any, cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from accounts.models import User

from .forms import ProgressPlanForm
from .models import ProgressPlan, WeightGoal
from .outlook import build_outlook, current_weight
from .profiles import profile_context


@login_required
@never_cache
@sensitive_post_parameters(
    "age", "height", "sex", "goal_weight", "maintenance_calories"
)
@require_http_methods(["GET", "POST"])
def outlook_view(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    plan = ProgressPlan.objects.filter(user=user).first()
    weight = current_weight(user)
    goal = WeightGoal.objects.filter(user=user).first()
    initial: dict[str, Any] = {"goal_weight": goal.weight if goal else None}
    shared = profile_context(user, reverse("tracker:outlook"))
    if plan is None:
        if shared["body_profile"]:
            initial["mode"] = shared["body_profile"].goal
        elif goal and goal.weight is not None and weight:
            initial["mode"] = "gain" if float(goal.weight) > weight["value"] else "lose"
    previous_mode = plan.mode if plan else None
    previous_maintenance = plan.maintenance_calories if plan else None
    form = ProgressPlanForm(
        request.POST if request.method == "POST" else None,
        instance=plan,
        initial=initial,
        current_weight=weight["value"] if weight else None,
    )
    if (
        request.method == "POST"
        and not shared["profile_needed"]
        and form.is_valid()
        and weight
    ):
        with transaction.atomic():
            updated = form.save(commit=False)
            updated.user = user
            goal_weight = form.cleaned_data["goal_weight"]
            if updated.mode == "maintain":
                goal_weight = (
                    goal.weight
                    if goal and previous_mode == "maintain" and goal.weight is not None
                    else Decimal(str(round(weight["value"], 2)))
                )
            if (
                plan is None
                or previous_mode != updated.mode
                or not goal
                or goal.weight != goal_weight
            ):
                updated.start_weight = Decimal(str(round(weight["value"], 2)))
                updated.started_on = timezone.localdate()
            if updated.maintenance_calories is not None and (
                plan is None
                or previous_maintenance != updated.maintenance_calories
                or updated.maintenance_weight is None
            ):
                updated.maintenance_weight = Decimal(str(round(weight["value"], 2)))
            updated.save()
            WeightGoal.objects.update_or_create(
                user=user, defaults={"weight": goal_weight}
            )
            shared["body_profile"].goal = updated.mode
            shared["body_profile"].save(update_fields=["goal", "updated_at"])
        messages.success(
            request,
            gettext(
                "Your outlook is ready. Your calorie targets have not been changed."
            ),
        )
        return redirect("tracker:outlook")
    weeks = request.GET.get("weeks", "26")
    horizon = int(weeks) if weeks in ("12", "26", "52", "104") else 26
    context = build_outlook(user, horizon)
    context.update(shared)
    if shared["profile_needed"]:
        context["ready"] = False
    context.update(
        {
            "form": form,
            "editing": request.GET.get("edit") == "1"
            or not context["ready"]
            or form.is_bound,
        }
    )
    return render(request, "tracker/outlook.html", context)
