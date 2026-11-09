from typing import Any

from django.contrib import admin, messages
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext, ngettext

from .admin_forms import AIModelDefaultsForm
from .models import (
    AIModelDefaults,
    BodyProfile,
    DailyTarget,
    Meal,
    MealEstimate,
    OpenRouterSettings,
    TargetEstimate,
    WeightEntry,
    WeightGoal,
)


@admin.register(AIModelDefaults)
class AIModelDefaultsAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    form = AIModelDefaultsForm
    list_display = ("__str__", "model", "updated_at")
    fields = ("model", "apply_to_existing", "updated_at")
    readonly_fields = ("updated_at",)
    actions = None

    def get_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is not None and not self.has_change_permission(request, obj):
            return ("model", "updated_at")
        return super().get_fields(request, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return (
            super().has_add_permission(request) and not AIModelDefaults.objects.exists()
        )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def save_model(
        self, request: HttpRequest, obj: AIModelDefaults, form: Any, change: bool
    ) -> None:
        super().save_model(request, obj, form, change)
        if form.cleaned_data.get("apply_to_existing"):
            count = OpenRouterSettings.objects.update(
                model=obj.model, updated_at=timezone.now()
            )
            self.message_user(
                request,
                ngettext(
                    "The default was saved and %(count)s existing user was switched to %(model)s.",
                    "The default was saved and %(count)s existing users were switched to %(model)s.",
                    count,
                )
                % {"count": count, "model": obj.model},
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                gettext(
                    "The default was saved for new AI setups. Existing users kept their selected models."
                ),
                messages.SUCCESS,
            )

    def construct_change_message(
        self, request: HttpRequest, form: Any, formsets: Any, add: bool = False
    ) -> Any:
        message = super().construct_change_message(request, form, formsets, add)
        if form.cleaned_data.get("apply_to_existing"):
            message.append(
                {"changed": {"fields": ["Applied the AI model to all existing users"]}}
            )
        return message


@admin.register(BodyProfile)
class BodyProfileAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "updated_at")
    search_fields = ("user__email",)


@admin.register(Meal)
class MealAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = ("estimate",)
    list_display = (
        "description",
        "user",
        "date",
        "time",
        "kind",
        "calories",
        "estimated",
    )
    list_filter = ("date", "kind")
    search_fields = ("description", "user__email")


@admin.register(MealEstimate)
class MealEstimateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "date", "status", "created_at")
    readonly_fields = (
        "original_input",
        "result",
        "submission_key",
        "created_at",
        "saved_at",
    )
    list_filter = ("status", "date")
    search_fields = ("user__email",)


@admin.register(TargetEstimate)
class TargetEstimateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "status", "created_at")
    readonly_fields = ("original_profile", "result", "created_at", "saved_at")
    search_fields = ("user__email",)


@admin.register(DailyTarget)
class DailyTargetAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "calories_min", "calories_max")
    search_fields = ("user__email",)


@admin.register(WeightEntry)
class WeightEntryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "date", "weight")
    list_filter = ("date",)
    search_fields = ("user__email",)


@admin.register(WeightGoal)
class WeightGoalAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "weight")
