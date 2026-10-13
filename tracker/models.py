from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

NUTRIENTS = ("calories", "protein", "carbs", "fat")


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
        BREAKFAST = "breakfast", "Breakfast"
        LUNCH = "lunch", "Lunch"
        DINNER = "dinner", "Dinner"
        SNACK = "snack", "Snack"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate, db_index=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    description = models.CharField(max_length=240)
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
