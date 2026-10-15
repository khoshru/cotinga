import uuid
from typing import Any, cast

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy

from .localization import english_text, translation_language
from .models import (
    NUTRIENTS,
    BodyProfile,
    DailyTarget,
    Meal,
    ProgressPlan,
    WeightEntry,
    WeightGoal,
)


class MealForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Meal
        fields: tuple[str, ...] = (
            "kind",
            "date",
            "time",
            "description",
            "description_fa",
            "description_ka",
            "portion",
            "portion_fa",
            "portion_ka",
            "calories",
            "protein",
            "carbs",
            "fat",
        )
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "description": forms.TextInput(
                attrs={"placeholder": gettext_lazy("What did you have?"), "dir": "auto"}
            ),
        }
        labels = {
            "date": gettext_lazy("Date"),
            "kind": gettext_lazy("Meal"),
            "description": gettext_lazy("What you ate"),
            "time": gettext_lazy("Time (optional)"),
            "portion": gettext_lazy("Portion (optional)"),
            "description_fa": gettext_lazy("What you ate (Farsi, optional)"),
            "portion_fa": gettext_lazy("Portion (Farsi, optional)"),
            "description_ka": gettext_lazy("What you ate (Georgian, optional)"),
            "portion_ka": gettext_lazy("Portion (Georgian, optional)"),
            "calories": gettext_lazy("Calories (kcal)"),
            "protein": gettext_lazy("Protein (g)"),
            "carbs": gettext_lazy("Carbs (g)"),
            "fat": gettext_lazy("Fat (g)"),
        }
        widgets["portion"] = forms.TextInput(attrs={"dir": "ltr"})
        widgets["description_fa"] = forms.TextInput(attrs={"dir": "rtl"})
        widgets["portion_fa"] = forms.TextInput(attrs={"dir": "rtl"})
        widgets["description_ka"] = forms.TextInput(attrs={"dir": "ltr"})
        widgets["portion_ka"] = forms.TextInput(attrs={"dir": "ltr"})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._source_text = {
            name: self.initial.get(name, getattr(self.instance, name))
            for name in ("description", "portion")
        }
        language = translation_language()
        if language:
            self.fields["description"].label = gettext_lazy("What you ate (English)")
            self.fields["portion"].label = gettext_lazy("Portion (English, optional)")
            self.fields["description"].help_text = gettext_lazy(
                "Keep the English version here. Add your translation below, or use AI to generate both."
            )
        for code in ("fa", "ka"):
            if code != language:
                for name in (f"description_{code}", f"portion_{code}"):
                    if name in self.initial:
                        setattr(self.instance, name, self.initial[name])
                    self.fields.pop(name, None)

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        for name in ("description", "portion"):
            if name not in cleaned:
                continue
            try:
                cleaned[name] = english_text(cleaned[name])
            except ValueError:
                self.add_error(
                    name,
                    gettext_lazy(
                        "Use English here. Add a translation in the separate field, or let AI generate both versions."
                    ),
                )
                continue
            if cleaned[name] != self._source_text[name]:
                for code in ("fa", "ka"):
                    if code != translation_language():
                        setattr(self.instance, f"{name}_{code}", "")
        return cleaned


class TargetForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = DailyTarget
        fields = tuple(f"{n}_{bound}" for n in NUTRIENTS for bound in ("min", "max"))
        labels = {
            "calories_min": gettext_lazy("Calories minimum (kcal)"),
            "calories_max": gettext_lazy("Calories maximum (kcal)"),
            "protein_min": gettext_lazy("Protein minimum (g)"),
            "protein_max": gettext_lazy("Protein maximum (g)"),
            "carbs_min": gettext_lazy("Carbs minimum (g)"),
            "carbs_max": gettext_lazy("Carbs maximum (g)"),
            "fat_min": gettext_lazy("Fat minimum (g)"),
            "fat_max": gettext_lazy("Fat maximum (g)"),
        }

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        for nutrient in NUTRIENTS:
            low, high = cleaned.get(f"{nutrient}_min"), cleaned.get(f"{nutrient}_max")
            if low is not None and high is not None and low > high:
                self.add_error(
                    f"{nutrient}_max",
                    gettext_lazy("Maximum must be at least the minimum."),
                )
        return cleaned


class EstimateForm(forms.Form):
    submission_id = forms.UUIDField(
        required=False, initial=uuid.uuid4, widget=forms.HiddenInput
    )
    date = forms.DateField(
        initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    prompt = forms.CharField(
        label=gettext_lazy("What did you eat?"),
        max_length=6000,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "dir": "auto",
                "placeholder": gettext_lazy(
                    "Describe one meal or your whole day, including times and portions you remember."
                ),
            }
        ),
    )

    def clean_prompt(self) -> str:
        prompt = str(self.cleaned_data["prompt"])
        if not prompt.strip():
            raise forms.ValidationError(gettext_lazy("Describe what you ate."))
        return prompt


class MealSaveForm(forms.Form):
    journal_state = forms.CharField(required=False, widget=forms.HiddenInput)
    confirm_replacement = forms.BooleanField(
        required=False,
        label=gettext_lazy("Replace the existing entries for these meal types."),
    )


class OpenRouterForm(forms.Form):
    api_key = forms.CharField(
        label=gettext_lazy("OpenRouter API key"),
        required=False,
        max_length=200,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy("Leave this blank to keep your saved key."),
    )
    model = forms.CharField(label=gettext_lazy("Model"), required=False, max_length=200)
    remove_key = forms.BooleanField(
        label=gettext_lazy("Remove my saved key"), required=False
    )

    def clean_api_key(self) -> str:
        key = str(self.cleaned_data.get("api_key", "")).strip()
        if key and (
            not key.startswith("sk-or-")
            or any(character.isspace() for character in key)
            or len(key) < 30
        ):
            raise forms.ValidationError(
                gettext_lazy("Enter a valid OpenRouter API key.")
            )
        return key

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("remove_key") and cleaned.get("api_key"):
            self.add_error(
                "api_key",
                gettext_lazy("Choose either a new key or removal of the saved key."),
            )
        return cleaned


class WeightForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WeightEntry
        fields = ("date", "weight", "notes")
        labels = {
            "date": gettext_lazy("Date"),
            "weight": gettext_lazy("Weight (kg)"),
            "notes": gettext_lazy("Note (optional)"),
        }
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "weight": forms.NumberInput(attrs={"step": "0.01"}),
        }

    def clean_date(self) -> Any:
        day = self.cleaned_data["date"]
        if day > timezone.localdate():
            raise forms.ValidationError(
                gettext_lazy("Choose today or a past date for a weigh-in.")
            )
        return day


class WeightGoalForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WeightGoal
        fields = ("weight",)
        labels = {"weight": gettext_lazy("Goal weight (kg, optional)")}
        widgets = {"weight": forms.NumberInput(attrs={"step": "0.01"})}


class StatisticsForm(forms.Form):
    period = forms.ChoiceField(
        required=False,
        choices=(
            ("7", gettext_lazy("Last 7 days")),
            ("30", gettext_lazy("Last 30 days")),
            ("90", gettext_lazy("Last 90 days")),
            ("365", gettext_lazy("Last year")),
            ("custom", gettext_lazy("Custom dates")),
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
                raise forms.ValidationError(
                    gettext_lazy("Choose both dates for a custom period.")
                )
            if end < start or (end - start).days > 365:
                raise forms.ValidationError(
                    gettext_lazy("Choose an ordered date range of up to one year.")
                )
            if end > timezone.localdate():
                raise forms.ValidationError(
                    gettext_lazy("Statistics can end today or on a past date.")
                )
        return cleaned


class TargetEstimateForm(forms.Form):
    age = forms.IntegerField(label=gettext_lazy("Age"), min_value=18, max_value=100)
    height = forms.IntegerField(
        label=gettext_lazy("Height (cm)"), min_value=100, max_value=250
    )
    weight = forms.DecimalField(
        label=gettext_lazy("Current weight (kg)"),
        min_value=1,
        max_value=500,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )
    sex = forms.ChoiceField(
        label=gettext_lazy("Sex for the energy estimate"),
        choices=[
            ("", gettext_lazy("Choose an option")),
            ("female", gettext_lazy("Female")),
            ("male", gettext_lazy("Male")),
        ],
    )
    activity = forms.ChoiceField(
        label=gettext_lazy("Usual activity"),
        choices=[
            ("sedentary", gettext_lazy("Mostly sitting")),
            ("light", gettext_lazy("Lightly active")),
            ("moderate", gettext_lazy("Moderately active")),
            ("very_active", gettext_lazy("Very active")),
        ],
    )
    goal = forms.ChoiceField(
        label=gettext_lazy("Your goal"),
        choices=[
            ("lose", gettext_lazy("Gradual fat loss")),
            ("maintain", gettext_lazy("Maintain weight")),
            ("gain", gettext_lazy("Gradual weight gain")),
        ],
    )
    notes = forms.CharField(
        label=gettext_lazy("Anything else to consider?"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "dir": "auto",
                "placeholder": gettext_lazy(
                    "Your routine, food preferences, training, or other useful context."
                ),
            }
        ),
    )


class ProgressPlanForm(forms.ModelForm):  # type: ignore[type-arg]
    goal_weight = forms.DecimalField(
        label=gettext_lazy("Goal weight (kg)"),
        required=False,
        min_value=20,
        max_value=350,
        max_digits=6,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.1"}),
    )

    class Meta:
        model = ProgressPlan
        fields = (
            "mode",
            "goal_weight",
            "maintenance_calories",
        )
        labels = {
            "mode": gettext_lazy("What are you working toward?"),
            "maintenance_calories": gettext_lazy(
                "Known maintenance calories (optional)"
            ),
        }
        widgets = {"mode": forms.RadioSelect()}
        help_texts = {
            "maintenance_calories": gettext_lazy(
                "If you know the calories that keep your weight steady, enter them. Otherwise, your saved profile is used."
            )
        }

    def __init__(
        self, *args: Any, current_weight: float | None = None, **kwargs: Any
    ) -> None:
        self.current_weight = current_weight
        super().__init__(*args, **kwargs)

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if self.current_weight is None:
            self.add_error(
                None, gettext_lazy("Log a weigh-in before building your outlook.")
            )
        elif not 20 <= self.current_weight <= 350:
            self.add_error(
                None,
                gettext_lazy("This planner supports adult weights from 20 to 350 kg."),
            )
        goal = cleaned.get("goal_weight")
        if cleaned.get("mode") != "maintain":
            if goal is None:
                self.add_error(
                    "goal_weight",
                    gettext_lazy("Add a goal weight for a loss or gain outlook."),
                )
            elif self.current_weight is not None:
                if (
                    cleaned.get("mode") == "lose"
                    and float(goal) > self.current_weight + 0.25
                ):
                    self.add_error(
                        "goal_weight",
                        gettext_lazy(
                            "Choose a goal below your current weight for a loss outlook."
                        ),
                    )
                if (
                    cleaned.get("mode") == "gain"
                    and float(goal) < self.current_weight - 0.25
                ):
                    self.add_error(
                        "goal_weight",
                        gettext_lazy(
                            "Choose a goal above your current weight for a gain outlook."
                        ),
                    )
        return cleaned


class BodyProfileForm(forms.ModelForm):  # type: ignore[type-arg]
    weight = forms.DecimalField(
        label=gettext_lazy("Current weight (kg)"),
        min_value=1,
        max_value=500,
        max_digits=6,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )

    class Meta:
        model = BodyProfile
        fields = ("age", "height", "sex", "activity", "weight", "goal", "notes")
        labels = {
            "age": gettext_lazy("Age"),
            "height": gettext_lazy("Height (cm)"),
            "sex": gettext_lazy("Sex for the energy estimate"),
            "activity": gettext_lazy("Usual activity"),
            "goal": gettext_lazy("Your goal"),
            "notes": gettext_lazy("Anything else to consider?"),
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3, "dir": "auto"})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in ("age", "height", "sex", "activity"):
            self.fields[name].required = True
        for name, choices in (
            ("sex", BodyProfile.Sex.choices),
            ("activity", BodyProfile.Activity.choices),
        ):
            cast(forms.ChoiceField, self.fields[name]).choices = [
                ("", gettext_lazy("Choose an option")),
                *choices,
            ]
