from typing import Any

from django import forms

from .models import NUTRIENTS, DailyTarget, Meal


class MealForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Meal
        fields = ("kind", "date", "description", "calories", "protein", "carbs", "fat")
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.TextInput(attrs={"placeholder": "What did you have?"}),
        }
        labels = {
            "kind": "Meal",
            "description": "What you ate",
            "calories": "Calories (kcal)",
            "protein": "Protein (g)",
            "carbs": "Carbs (g)",
            "fat": "Fat (g)",
        }


class TargetForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = DailyTarget
        fields = tuple(f"{n}_{bound}" for n in NUTRIENTS for bound in ("min", "max"))
        labels = {
            f"{n}_{bound}": f"{n.title()} {'minimum' if bound == 'min' else 'maximum'} ({'kcal' if n == 'calories' else 'g'})"
            for n in NUTRIENTS
            for bound in ("min", "max")
        }

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        for nutrient in NUTRIENTS:
            low, high = cleaned.get(f"{nutrient}_min"), cleaned.get(f"{nutrient}_max")
            if low is not None and high is not None and low > high:
                self.add_error(
                    f"{nutrient}_max", "Maximum must be at least the minimum."
                )
        return cleaned
