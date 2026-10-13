from django.contrib import admin

from .models import DailyTarget, Meal


@admin.register(Meal)
class MealAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("description", "user", "date", "kind", "calories")
    list_filter = ("date", "kind")
    search_fields = ("description", "user__email")


@admin.register(DailyTarget)
class DailyTargetAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "calories_min", "calories_max")
    search_fields = ("user__email",)
