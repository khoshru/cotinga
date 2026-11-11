import uuid
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from .forms import RoutineExerciseForm
from .models import Exercise, RoutineExercise


class RoutineRequestForm(forms.Form):
    submission = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    description = forms.CharField(
        label=_("Tell us what you want"),
        max_length=2000,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "dir": "auto",
                "placeholder": _(
                    "For example: three days to build strength, with dumbbells and a bench. I prefer full-body workouts and dislike jumping."
                ),
            }
        ),
    )
    days = forms.TypedChoiceField(
        label=_("Training days per week"),
        choices=[(x, str(x)) for x in range(1, 8)],
        coerce=int,
        initial=3,
    )
    minutes = forms.TypedChoiceField(
        label=_("Minutes per workout"),
        choices=[(x, str(x)) for x in (15, 20, 30, 45, 60, 75, 90, 120)],
        coerce=int,
        initial=45,
    )
    experience = forms.ChoiceField(
        label=_("Training experience"),
        choices=[
            ("beginner", _("Beginner")),
            ("intermediate", _("Intermediate")),
            ("advanced", _("Advanced")),
        ],
        initial="beginner",
    )
    equipment = forms.MultipleChoiceField(
        label=_("Available equipment"),
        choices=[
            (key, value)
            for key, value in Exercise.Equipment.choices
            if key != Exercise.Equipment.BODYWEIGHT
        ]
        + [
            ("bench", _("Bench or sturdy seat")),
            ("pullup_bar", _("Pull-up bar")),
            ("dip_bars", _("Dip bars")),
            ("box", _("Step or exercise box")),
            ("rack", _("Squat rack")),
            ("band_anchor", _("Secure resistance-band anchor")),
            ("ab_wheel", _("Ab wheel")),
            ("foot_anchor", _("Nordic curl foot anchor")),
            ("back_extension_bench", _("Back extension bench")),
            ("jump_rope", _("Jump rope")),
            ("battle_ropes", _("Battle ropes and secure anchor")),
        ],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    include_custom = forms.BooleanField(
        label=_("Include my custom exercises"), required=False, initial=True
    )

    def clean_description(self) -> str:
        description = str(self.cleaned_data["description"])
        if not description.strip():
            raise forms.ValidationError(
                self.fields["description"].error_messages["required"], code="required"
            )
        return description


class RoutineDayForm(forms.Form):
    keep = forms.BooleanField(
        label=_("Save this routine"), required=False, initial=True
    )
    name = forms.CharField(
        label=_("Routine name"),
        max_length=100,
        widget=forms.TextInput(attrs={"dir": "auto"}),
    )
    notes = forms.CharField(
        label=_("Routine notes"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2, "dir": "auto"}),
    )


class SuggestedExerciseForm(RoutineExerciseForm):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        del self.fields["weight"]
        del self.fields["group"]
        del self.fields["notes"]
        self.fields["sets"] = forms.IntegerField(
            label=_("Sets"), min_value=1, max_value=6
        )


class SuggestedExerciseSet(forms.BaseFormSet):  # type: ignore[type-arg]
    def __init__(self, *args: Any, exercises: list[Exercise], **kwargs: Any) -> None:
        self.exercises = exercises
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index: int | None) -> dict[str, Any]:
        kwargs = super().get_form_kwargs(index)
        kwargs["instance"] = RoutineExercise(
            exercise=self.exercises[min(index or 0, len(self.exercises) - 1)]
        )
        return kwargs

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        kept = [
            form
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if sum(form.cleaned_data["sets"] for form in kept) > 48:
            raise forms.ValidationError(
                _("Keep at most 48 sets in one suggested workout.")
            )
