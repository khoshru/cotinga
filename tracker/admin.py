from django.contrib import admin

from .models import (
    BodyProfile,
    DailyTarget,
    Meal,
    MealEstimate,
    TargetEstimate,
    WeightEntry,
    WeightGoal,
)


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
