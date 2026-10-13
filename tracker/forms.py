from typing import Any

from django import forms
from django.utils import timezone

from .models import NUTRIENTS, DailyTarget, Meal, WeightEntry, WeightGoal


class MealForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Meal
        fields: tuple[str, ...] = (
            "kind",
            "date",
            "time",
            "description",
            "portion",
            "calories",
            "protein",
            "carbs",
            "fat",
        )
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "description": forms.TextInput(attrs={"placeholder": "What did you have?"}),
        }
        labels = {
            "kind": "Meal",
            "description": "What you ate",
            "time": "Time (optional)",
            "portion": "Portion (optional)",
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


class EstimateForm(forms.Form):
    date = forms.DateField(
        initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    prompt = forms.CharField(
        label="What did you eat?",
        max_length=6000,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "Describe one meal or your whole day, including times and portions you remember.",
            }
        ),
    )


class OpenRouterForm(forms.Form):
    api_key = forms.CharField(
        label="OpenRouter API key",
        required=False,
        max_length=200,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave this blank to keep your saved key.",
    )
    model = forms.CharField(label="Model", required=False, max_length=200)
    remove_key = forms.BooleanField(label="Remove my saved key", required=False)

    def clean_api_key(self) -> str:
        key = str(self.cleaned_data.get("api_key", "")).strip()
        if key and (
            not key.startswith("sk-or-")
            or any(character.isspace() for character in key)
            or len(key) < 30
        ):
            raise forms.ValidationError("Enter a valid OpenRouter API key.")
        return key

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("remove_key") and cleaned.get("api_key"):
            self.add_error(
                "api_key", "Choose either a new key or removal of the saved key."
            )
        return cleaned


class WeightForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WeightEntry
        fields = ("date", "weight", "notes")
        labels = {"weight": "Weight (kg)", "notes": "Note (optional)"}
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "weight": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def clean_date(self) -> Any:
        day = self.cleaned_data["date"]
        if day > timezone.localdate():
            raise forms.ValidationError("Choose today or a past date for a weigh-in.")
        return day


class WeightGoalForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WeightGoal
        fields = ("weight",)
        labels = {"weight": "Goal weight (kg, optional)"}
        widgets = {"weight": forms.NumberInput(attrs={"step": "0.01"})}


class StatisticsForm(forms.Form):
    period = forms.ChoiceField(
        required=False,
        choices=(
            ("7", "Last 7 days"),
            ("30", "Last 30 days"),
            ("90", "Last 90 days"),
            ("365", "Last year"),
            ("custom", "Custom dates"),
        ),
    )
    start = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    end = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("period") == "custom":
            start, end = cleaned.get("start"), cleaned.get("end")
            if not start or not end:
                raise forms.ValidationError("Choose both dates for a custom period.")
            if end < start or (end - start).days > 365:
                raise forms.ValidationError(
                    "Choose an ordered date range of up to one year."
                )
            if end > timezone.localdate():
                raise forms.ValidationError(
                    "Statistics can end today or on a past date."
                )
        return cleaned
