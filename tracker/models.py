import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy

from .localization import localized_text

NUTRIENTS = ("calories", "protein", "carbs", "fat")
DEFAULT_AI_MODEL = "google/gemini-4-argon"


class DailyTarget(models.Model):
    """User-editable daily calorie and nutrient ranges."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    calories_min = models.PositiveIntegerField(
        default=1750, validators=[MinValueValidator(1), MaxValueValidator(10000)]
    )
    calories_max = models.PositiveIntegerField(
        default=1850, validators=[MinValueValidator(1), MaxValueValidator(10000)]
    )
    protein_min = models.PositiveIntegerField(
        default=115, validators=[MaxValueValidator(1000)]
    )
    protein_max = models.PositiveIntegerField(
        default=140, validators=[MaxValueValidator(1000)]
    )
    carbs_min = models.PositiveIntegerField(
        default=170, validators=[MaxValueValidator(1000)]
    )
    carbs_max = models.PositiveIntegerField(
        default=210, validators=[MaxValueValidator(1000)]
    )
    fat_min = models.PositiveIntegerField(
        default=55, validators=[MaxValueValidator(1000)]
    )
    fat_max = models.PositiveIntegerField(
        default=60, validators=[MaxValueValidator(1000)]
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(**{f"{n}_min__lte": models.F(f"{n}_max")}),
                name=f"target_{n}_ordered",
            )
            for n in NUTRIENTS
        ]

    def __str__(self) -> str:
        return f"Targets for {self.user}"


class Meal(models.Model):
    class Kind(models.TextChoices):
        BREAKFAST = "breakfast", gettext_lazy("Breakfast")
        LUNCH = "lunch", gettext_lazy("Lunch")
        DINNER = "dinner", gettext_lazy("Dinner")
        SNACK = "snack", gettext_lazy("Snack")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate, db_index=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    description = models.CharField(max_length=240)
    description_fa = models.CharField(max_length=240, blank=True)
    time = models.TimeField(null=True, blank=True)
    portion = models.CharField(max_length=160, blank=True)
    portion_fa = models.CharField(max_length=160, blank=True)
    estimated = models.BooleanField(default=False)
    estimate_model = models.CharField(max_length=200, blank=True)
    calories = models.PositiveIntegerField(validators=[MaxValueValidator(10000)])
    protein = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(1000)]
    )
    carbs = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(1000)])
    fat = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(1000)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["user", "date"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.description}"

    def clean(self) -> None:
        super().clean()
        if (
            self.user_id
            and self.date
            and self.kind
            and Meal.objects.filter(
                user_id=self.user_id, date=self.date, kind=self.kind
            )
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError(
                {
                    "kind": gettext_lazy(
                        "This meal type is already logged for this day. Edit the existing entry, or use AI review to replace it."
                    )
                }
            )

    @property
    def display_description(self) -> str:
        return localized_text(self.description, self.description_fa)

    @property
    def display_portion(self) -> str:
        return localized_text(self.portion, self.portion_fa)


class OpenRouterSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    encrypted_api_key = models.TextField(blank=True, editable=False)
    model = models.CharField(max_length=200, blank=True, default=DEFAULT_AI_MODEL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"OpenRouter settings for {self.user}"

    @property
    def has_key(self) -> bool:
        return bool(self.encrypted_api_key)


class MealEstimate(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        SAVED = "saved", "Saved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField()
    model = models.CharField(max_length=200)
    submission_key = models.CharField(
        max_length=64, blank=True, default="", editable=False
    )
    result = models.JSONField(default=dict)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PROCESSING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "submission_key"],
                condition=~models.Q(submission_key=""),
                name="unique_meal_submission",
            )
        ]

    def __str__(self) -> str:
        return f"Meal estimate for {self.date} ({self.get_status_display()})"


class WeightEntry(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate)
    weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
    )
    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-date", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "date"], name="weight_user_date_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.date}: {self.weight} kg"


class WeightGoal(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
    )

    def __str__(self) -> str:
        return f"Weight goal for {self.user}"


class TargetEstimate(models.Model):
    """A validated target suggestion awaiting the user's review."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    model = models.CharField(max_length=200)
    result = models.JSONField(default=dict)
    status = models.CharField(
        max_length=12,
        choices=MealEstimate.Status.choices,
        default=MealEstimate.Status.PROCESSING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        return f"Target estimate ({self.get_status_display()})"


class ProgressPlan(models.Model):
    """Private inputs for deterministic weight outlooks; targets stay user-controlled."""

    class Goal(models.TextChoices):
        LOSE = "lose", gettext_lazy("Lose weight")
        GAIN = "gain", gettext_lazy("Gain weight")
        MAINTAIN = "maintain", gettext_lazy("Maintain weight")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    mode = models.CharField(max_length=8, choices=Goal.choices, default=Goal.LOSE)
    maintenance_calories = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1000), MaxValueValidator(6000)],
    )
    maintenance_weight = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    start_weight = models.DecimalField(max_digits=6, decimal_places=2)
    started_on = models.DateField(default=timezone.localdate)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Progress plan for {self.user}"


class BodyProfile(models.Model):
    """One private source of body details for targets and weight calculations."""

    class Sex(models.TextChoices):
        FEMALE = "female", gettext_lazy("Female")
        MALE = "male", gettext_lazy("Male")

    class Activity(models.TextChoices):
        SEDENTARY = "sedentary", gettext_lazy("Mostly sitting")
        LIGHT = "light", gettext_lazy("Lightly active")
        MODERATE = "moderate", gettext_lazy("Moderately active")
        VERY_ACTIVE = "very_active", gettext_lazy("Very active")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    age = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(18), MaxValueValidator(100)],
    )
    height = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(100), MaxValueValidator(250)],
    )
    sex = models.CharField(max_length=6, choices=Sex.choices, blank=True)
    activity = models.CharField(max_length=12, choices=Activity.choices, blank=True)
    goal = models.CharField(
        max_length=8, choices=ProgressPlan.Goal.choices, default=ProgressPlan.Goal.LOSE
    )
    notes = models.TextField(blank=True, max_length=2000)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sex__in=["", "female", "male"]),
                name="profile_valid_sex",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    activity__in=["", "sedentary", "light", "moderate", "very_active"]
                ),
                name="profile_valid_activity",
            ),
        ]

    def __str__(self) -> str:
        return f"Body profile for {self.user}"

    @property
    def complete(self) -> bool:
        return bool(
            self.age
            and self.height
            and self.sex in self.Sex.values
            and self.activity in self.Activity.values
        )
