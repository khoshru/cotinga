import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from tracker.localization import localized_text

from .catalog import (
    FIXTURE_LABELS,
    FOCUS_LABELS,
    KIND_LABELS,
    equipment_requirements,
    movement_details,
)
from .media import exercise_media


class Exercise(models.Model):
    class Muscle(models.TextChoices):
        CHEST = "chest", _("Chest")
        BACK = "back", _("Back")
        SHOULDERS = "shoulders", _("Shoulders")
        BICEPS = "biceps", _("Biceps")
        TRICEPS = "triceps", _("Triceps")
        FOREARMS = "forearms", _("Forearms")
        QUADS = "quads", _("Quadriceps")
        HAMSTRINGS = "hamstrings", _("Hamstrings")
        GLUTES = "glutes", _("Glutes")
        CALVES = "calves", _("Calves")
        CORE = "core", _("Core")
        FULL_BODY = "full_body", _("Full body")
        CARDIO = "cardio", _("Cardio")

    class Equipment(models.TextChoices):
        BARBELL = "barbell", _("Barbell")
        DUMBBELL = "dumbbell", _("Dumbbell")
        MACHINE = "machine", _("Machine")
        CABLE = "cable", _("Cable")
        BODYWEIGHT = "bodyweight", _("Bodyweight")
        KETTLEBELL = "kettlebell", _("Kettlebell")
        BAND = "band", _("Resistance band")
        OTHER = "other", _("Other equipment")

    class Mode(models.TextChoices):
        WEIGHT = "weight_reps", _("Weight and reps")
        BODYWEIGHT = "bodyweight_reps", _("Bodyweight and reps")
        ASSISTED = "assisted_reps", _("Assistance and reps")
        REPS = "reps", _("Reps only")
        DURATION = "duration", _("Time only")
        CARDIO = "distance_time", _("Distance and time")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True
    )
    slug = models.SlugField(max_length=150, unique=True, null=True, blank=True)
    name = models.CharField(max_length=120)
    name_fa = models.CharField(max_length=160, blank=True)
    name_ka = models.CharField(max_length=160, blank=True)
    muscle = models.CharField(max_length=16, choices=Muscle.choices)
    equipment = models.CharField(max_length=12, choices=Equipment.choices)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.WEIGHT)
    notes = models.TextField(max_length=1000, blank=True)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(owner__isnull=False) | models.Q(slug__isnull=False),
                name="gym_catalog_or_owned_exercise",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return localized_text(self.name, self.name_fa, self.name_ka)

    @property
    def media(self) -> dict[str, object]:
        return (
            exercise_media().get(self.slug or "", {}) if self.owner_id is None else {}
        )

    @property
    def details(self) -> dict[str, object]:
        return (
            dict(movement_details().get(self.slug or "", {}))
            if self.owner_id is None
            else {}
        )

    @property
    def focus_label(self) -> str:
        return str(FOCUS_LABELS.get(str(self.details.get("focus", "")), ""))

    @property
    def kind_label(self) -> str:
        return str(KIND_LABELS.get(str(self.details.get("kind", "")), ""))

    @property
    def display_cue(self) -> str:
        return localized_text(
            *[str(self.details.get(field, "")) for field in ("cue", "cue_fa", "cue_ka")]
        )

    @property
    def fixtures(self) -> list[str]:
        return [
            str(FIXTURE_LABELS[key]) for key in sorted(equipment_requirements(self))
        ]


class GymPreferences(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    weekly_goal = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(7)]
    )

    def __str__(self) -> str:
        return f"Training preferences for {self.user}"


class Routine(models.Model):
    ai_suggestion = models.ForeignKey(
        "RoutineSuggestion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routines",
    )
    name_fa = models.CharField(max_length=100, blank=True, default="", db_default="")
    name_ka = models.CharField(max_length=100, blank=True, default="", db_default="")
    notes_fa = models.TextField(max_length=1000, blank=True, default="", db_default="")
    notes_ka = models.TextField(max_length=1000, blank=True, default="", db_default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    notes = models.TextField(max_length=1000, blank=True)
    archived = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]

    def __str__(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return localized_text(self.name, self.name_fa, self.name_ka)

    @property
    def display_notes(self) -> str:
        return localized_text(self.notes, self.notes_fa, self.notes_ka)


class RoutineExercise(models.Model):
    routine = models.ForeignKey(
        Routine, on_delete=models.CASCADE, related_name="exercises"
    )
    exercise = models.ForeignKey(Exercise, on_delete=models.RESTRICT)
    position = models.PositiveIntegerField(default=0)
    sets = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    reps = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(1000)]
    )
    weight = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(2000)],
    )
    seconds = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(86400)]
    )
    distance = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(1000)],
    )
    rest_seconds = models.PositiveSmallIntegerField(
        default=90, validators=[MaxValueValidator(1800)]
    )
    group = models.CharField(
        max_length=1,
        blank=True,
        choices=[("", _("No superset")), *[(x, x) for x in "ABCD"]],
    )
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self) -> str:
        return f"{self.routine}: {self.exercise}"


class Workout(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("In progress")
        FINISHED = "finished", _("Finished")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    routine = models.ForeignKey(
        Routine, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Preserve routine record comparisons after a routine is deleted.
    routine_key = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    name = models.CharField(max_length=100)
    date = models.DateField(default=timezone.localdate)
    notes = models.TextField(max_length=2000, blank=True)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.ACTIVE
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_seconds = models.PositiveIntegerField(default=0)
    bodyweight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
    )
    bodyweight_date = models.DateField(null=True, blank=True)
    rest_ends_at = models.DateTimeField(null=True, blank=True)
    rest_remaining = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(1800)]
    )
    version = models.PositiveIntegerField(default=0)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(status="active", archived_at__isnull=True),
                name="gym_one_active_workout",
            )
        ]
        indexes = [models.Index(fields=["user", "date"])]

    def __str__(self) -> str:
        return self.name

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished_at or self.paused_at or timezone.now()
        return max(
            0, int((end - self.started_at).total_seconds()) - self.paused_seconds
        )


class WorkoutExercise(models.Model):
    workout = models.ForeignKey(
        Workout, on_delete=models.CASCADE, related_name="exercises"
    )
    exercise = models.ForeignKey(Exercise, on_delete=models.RESTRICT)
    # Keep historical labels and tracking rules even if a custom exercise changes.
    name = models.CharField(max_length=120)
    name_fa = models.CharField(max_length=160, blank=True)
    name_ka = models.CharField(max_length=160, blank=True)
    muscle = models.CharField(max_length=16, choices=Exercise.Muscle.choices)
    equipment = models.CharField(max_length=12, choices=Exercise.Equipment.choices)
    mode = models.CharField(max_length=16, choices=Exercise.Mode.choices)
    position = models.PositiveIntegerField(default=0)
    rest_seconds = models.PositiveSmallIntegerField(
        default=90, validators=[MaxValueValidator(1800)]
    )
    group = models.CharField(
        max_length=1,
        blank=True,
        choices=RoutineExercise._meta.get_field("group").choices,
    )
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return localized_text(self.name, self.name_fa, self.name_ka)


class WorkoutSet(models.Model):
    class Kind(models.TextChoices):
        WORKING = "working", _("Working set")
        WARMUP = "warmup", _("Warm-up")
        DROP = "drop", _("Drop set")
        FAILURE = "failure", _("To failure")

    workout_exercise = models.ForeignKey(
        WorkoutExercise, on_delete=models.CASCADE, related_name="sets"
    )
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.WORKING)
    weight = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(2000)],
    )
    reps = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
    )
    seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(86400)],
    )
    distance = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(1000)],
    )
    rpe = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self) -> str:
        return f"{self.workout_exercise}: set {self.position + 1}"


class RoutineSuggestion(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", _("Preparing")
        READY = "ready", _("Ready to review")
        FAILED = "failed", _("Could not prepare")
        SAVED = "saved", _("Saved")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    submission = models.UUIDField(default=uuid.uuid4, editable=False)
    original_input = models.JSONField(default=dict, editable=False)
    language = models.CharField(max_length=2, default="en")
    model = models.CharField(max_length=200)
    result = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PROCESSING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "submission"], name="gym_unique_ai_submission"
            )
        ]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        return f"Routine suggestion ({self.get_status_display()})"
