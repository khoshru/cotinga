from typing import Any, cast

from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import User
from tracker.localization import translation_language

from .models import (
    Exercise,
    GymPreferences,
    Routine,
    RoutineExercise,
    Workout,
    WorkoutExercise,
    WorkoutSet,
)


class SecondsField(forms.CharField):
    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, int):
            return f"{value // 60:02}:{value % 60:02}"
        return value

    def clean(self, value: Any) -> int | None:
        raw = str(super().clean(value) or "").strip()
        if not raw:
            return None
        try:
            parts = raw.split(":")
            if (
                len(parts) == 2
                and all(x.isdecimal() for x in parts)
                and int(parts[1]) < 60
            ):
                seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 1 and raw.isdecimal():
                seconds = int(raw)
            else:
                raise ValueError
            if not 1 <= seconds <= 86400:
                raise ValueError
        except ValueError as exc:
            raise forms.ValidationError(
                _(
                    "Enter minutes:seconds, such as 02:30, or a number of seconds (up to 24 hours)."
                )
            ) from exc
        return seconds


class ExerciseForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Exercise
        fields = ("name", "muscle", "equipment", "mode", "notes")
        labels = {
            "name": _("Exercise name"),
            "muscle": _("Primary muscle"),
            "equipment": _("Equipment"),
            "mode": _("What to track"),
            "notes": _("Your exercise notes"),
        }
        widgets = {
            "notes": forms.Textarea(
                attrs={"rows": 3, "class": "gym-small-text", "dir": "auto"}
            ),
            "name": forms.TextInput(attrs={"dir": "auto"}),
        }

    def clean_mode(self) -> str:
        mode = str(self.cleaned_data["mode"])
        if (
            self.instance.pk
            and self.instance.mode != mode
            and self.instance.workoutexercise_set.exists()
        ):
            raise forms.ValidationError(
                _(
                    "This exercise already has workout history. Create a separate exercise to use different tracking rules."
                )
            )
        return mode


class RoutineForm(forms.ModelForm):  # type: ignore[type-arg]
    exercises = forms.ModelMultipleChoiceField(
        queryset=Exercise.objects.none(),
        required=False,
        label=_("Choose exercises"),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args: Any, user: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["exercises"].queryset = Exercise.objects.filter(  # type: ignore[attr-defined]
            Q(owner__isnull=True) | Q(owner=user), archived=False
        )
        self._canonical_text: dict[str, str] = {}
        self._language = translation_language()
        if self.instance.pk:
            del self.fields["exercises"]
            if self._language:
                for name in ("name", "notes"):
                    translated = getattr(self.instance, f"{name}_{self._language}", "")
                    if translated:
                        self._canonical_text[name] = str(getattr(self.instance, name))
                        self.initial[name] = translated

    def clean_exercises(self) -> Any:
        selected = self.cleaned_data["exercises"]
        if len(selected) > 40:
            raise forms.ValidationError(_("A routine can contain up to 40 exercises."))
        return selected

    def save(self, commit: bool = True) -> Routine:
        instance = cast(Routine, super().save(commit=False))
        for field in ("name", "notes"):
            if field in self._canonical_text:
                value = self.cleaned_data[field]
                setattr(instance, field, self._canonical_text[field] if value else "")
                setattr(instance, f"{field}_{self._language}", value)
            elif field in self.changed_data:
                setattr(instance, f"{field}_fa", "")
                setattr(instance, f"{field}_ka", "")
        if commit:
            instance.save()
        return instance

    class Meta:
        model = Routine
        fields = ("name", "notes")
        labels = {"name": _("Routine name"), "notes": _("Routine notes")}
        widgets = {
            "notes": forms.Textarea(
                attrs={"rows": 2, "class": "gym-small-text", "dir": "auto"}
            ),
            "name": forms.TextInput(attrs={"dir": "auto"}),
        }


class RoutineExerciseForm(forms.ModelForm):  # type: ignore[type-arg]
    seconds = SecondsField(label=_("Time (mm:ss)"), initial=60)

    class Meta:
        model = RoutineExercise
        fields = (
            "sets",
            "reps",
            "weight",
            "seconds",
            "distance",
            "rest_seconds",
            "group",
            "notes",
        )
        labels = {
            "sets": _("Sets"),
            "reps": _("Target reps"),
            "weight": _("Weight (kg)"),
            "distance": _("Distance (km)"),
            "rest_seconds": _("Rest (seconds)"),
            "group": _("Superset"),
            "notes": _("Exercise notes"),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        mode = self.instance.exercise.mode
        for field in ("reps", "weight", "seconds", "distance"):
            if field not in mode_fields(mode):
                self.fields[field].widget = forms.HiddenInput()
                self.fields[field].required = False
        if mode == Exercise.Mode.ASSISTED:
            self.fields["weight"].label = _("Assistance (kg)")
        elif mode == Exercise.Mode.BODYWEIGHT:
            self.fields["weight"].label = _("Added weight (kg)")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        # Required model defaults for fields that are not relevant to this mode.
        if data.get("reps") is None:
            data["reps"] = 10
        if data.get("seconds") is None:
            data["seconds"] = 60
        return data


def mode_fields(mode: str) -> tuple[str, ...]:
    fields: dict[str, tuple[str, ...]] = {
        Exercise.Mode.WEIGHT: ("weight", "reps"),
        Exercise.Mode.BODYWEIGHT: ("weight", "reps"),
        Exercise.Mode.ASSISTED: ("weight", "reps"),
        Exercise.Mode.REPS: ("reps",),
        Exercise.Mode.DURATION: ("seconds",),
        Exercise.Mode.CARDIO: ("distance", "seconds"),
    }
    return fields[mode]


class SetForm(forms.ModelForm):  # type: ignore[type-arg]
    completed = forms.BooleanField(required=False, label=_("Set complete"))
    seconds = SecondsField(
        label=_("Time (mm:ss)"),
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "02:30", "inputmode": "numeric", "dir": "ltr"}
        ),
    )

    class Meta:
        model = WorkoutSet
        fields = ("kind", "weight", "reps", "seconds", "distance", "rpe")
        labels = {
            "kind": _("Set type"),
            "weight": _("kg"),
            "reps": _("Reps"),
            "distance": _("km"),
            "rpe": _("Effort / RPE (1–10)"),
        }
        widgets = {
            "weight": forms.NumberInput(
                attrs={"step": ".01", "inputmode": "decimal", "dir": "ltr"}
            ),
            "distance": forms.NumberInput(
                attrs={"step": ".01", "inputmode": "decimal", "dir": "ltr"}
            ),
            "reps": forms.NumberInput(attrs={"inputmode": "numeric", "dir": "ltr"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.initial["completed"] = self.instance.completed_at is not None
        row = self.instance.workout_exercise
        for field in ("weight", "reps", "seconds", "distance"):
            self.fields[field].required = False
            if field not in mode_fields(row.mode):
                del self.fields[field]
        if row.mode == Exercise.Mode.ASSISTED:
            self.fields["weight"].label = _("Assistance kg")
        elif row.mode == Exercise.Mode.BODYWEIGHT:
            self.fields["weight"].label = _("Added kg")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        for field in ("weight", "reps", "seconds", "distance"):
            if field not in mode_fields(self.instance.workout_exercise.mode):
                data[field] = None
        if not data.get("completed"):
            return data
        row = self.instance.workout_exercise
        if (
            row.mode in (Exercise.Mode.BODYWEIGHT, Exercise.Mode.ASSISTED)
            and row.workout.bodyweight is None
        ):
            raise forms.ValidationError(
                _("Add your session bodyweight before completing this set.")
            )
        if row.mode == Exercise.Mode.BODYWEIGHT and data.get("weight") is None:
            data["weight"] = 0
        for field in mode_fields(row.mode):
            if data.get(field) is None and field not in self.errors:
                self.add_error(field, _("Enter a value before completing this set."))
        if (
            row.mode == Exercise.Mode.ASSISTED
            and data.get("weight") is not None
            and row.workout.bodyweight is not None
            and data["weight"] > row.workout.bodyweight
        ):
            self.add_error(
                "weight", _("Assistance cannot exceed your session bodyweight.")
            )
        return data


class StartForm(forms.Form):
    name = forms.CharField(label=_("Workout name"), max_length=100, required=False)
    date = forms.DateField(
        label=_("Workout date"),
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    routine = forms.ModelChoiceField(
        queryset=Routine.objects.none(), required=False, widget=forms.HiddenInput
    )

    def __init__(self, *args: Any, user: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["routine"] = forms.ModelChoiceField(
            queryset=Routine.objects.filter(user=user, archived=False),
            required=False,
            widget=forms.HiddenInput,
        )

    def clean_date(self) -> Any:
        day = self.cleaned_data["date"]
        if day > timezone.localdate():
            raise forms.ValidationError(_("Choose today or an earlier date."))
        return day


class WorkoutForm(forms.ModelForm):  # type: ignore[type-arg]
    duration = SecondsField(label=_("Correct duration (mm:ss)"), required=False)

    class Meta:
        model = Workout
        fields = ("name", "date", "notes")
        labels = {
            "name": _("Workout name"),
            "date": _("Workout date"),
            "notes": _("Workout notes"),
        }
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(
                attrs={"rows": 2, "class": "gym-small-text", "dir": "auto"}
            ),
        }

    def clean_date(self) -> Any:
        return StartForm.clean_date(self)  # type: ignore[arg-type]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.status != Workout.Status.FINISHED:
            del self.fields["duration"]
        else:
            self.initial["duration"] = max(1, self.instance.elapsed_seconds)


class WorkoutExerciseForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WorkoutExercise
        fields = ("rest_seconds", "group", "notes")
        labels = {
            "rest_seconds": _("Rest (seconds)"),
            "group": _("Superset"),
            "notes": _("Exercise notes"),
        }


class BodyweightForm(forms.ModelForm):  # type: ignore[type-arg]
    save_weigh_in = forms.BooleanField(
        label=_("Also save this to my weight journal"), required=False
    )

    class Meta:
        model = Workout
        fields = ("bodyweight",)
        labels = {"bodyweight": _("Bodyweight for this workout (kg)")}
        widgets = {
            "bodyweight": forms.NumberInput(
                attrs={"step": ".01", "inputmode": "decimal"}
            )
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["bodyweight"].required = True


class PreferencesForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = GymPreferences
        fields = ("weekly_goal",)
        labels = {"weekly_goal": _("Workouts per week")}
