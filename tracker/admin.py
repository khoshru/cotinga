from django.contrib import admin

from .models import DailyTarget, Meal, WeightEntry, WeightGoal


@admin.register(Meal)
class MealAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
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
