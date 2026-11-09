from django.contrib import admin

from .models import Exercise, Routine, Workout, WorkoutSet


@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "owner", "muscle", "equipment", "mode", "archived")
    list_filter = ("muscle", "equipment", "mode", "archived")
    search_fields = ("name", "name_fa", "name_ka", "owner__email")


@admin.register(Routine)
class RoutineAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "user", "archived", "updated_at")
    list_filter = ("archived",)
    search_fields = ("name", "user__email")


@admin.register(Workout)
class WorkoutAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "user", "date", "status", "bodyweight", "archived_at")
    list_filter = ("status", "date")
    search_fields = ("name", "user__email")
    readonly_fields = ("version", "rest_ends_at", "rest_remaining")


@admin.register(WorkoutSet)
class WorkoutSetAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "workout_exercise",
        "kind",
        "weight",
        "reps",
        "seconds",
        "distance",
        "completed_at",
    )
    list_filter = ("kind", "workout_exercise__mode")
    search_fields = ("workout_exercise__name", "workout_exercise__workout__user__email")
    list_select_related = ("workout_exercise__workout",)
