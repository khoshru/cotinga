from typing import Any

from django import forms
from django.utils.translation import gettext_lazy

from . import openrouter
from .models import AIModelDefaults


class AIModelWidget(forms.TextInput):
    template_name = "admin/tracker/ai_model_widget.html"

    def __init__(self) -> None:
        super().__init__(
            attrs={
                "list": "ai-model-options",
                "class": "vTextField",
                "autocomplete": "off",
                "placeholder": gettext_lazy("Search by model name or ID"),
            }
        )
        self.models: list[openrouter.ModelInfo] = []

    def get_context(self, name: str, value: Any, attrs: Any) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        context["models"] = self.models
        return context


class AIModelDefaultsForm(forms.ModelForm):  # type: ignore[type-arg]
    apply_to_existing = forms.BooleanField(
        label=gettext_lazy("Switch all existing users to this model"),
        required=False,
        initial=True,
        help_text=gettext_lazy(
            "Replaces every saved model choice, including custom selections. API keys and past results stay unchanged. Users can choose another model again later."
        ),
    )

    class Meta:
        model = AIModelDefaults
        fields = ("model",)
        widgets = {"model": AIModelWidget}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.catalog_error = ""
        self.available_models: list[openrouter.ModelInfo] = []
        if "model" not in self.fields:
            return
        try:
            self.available_models = openrouter.models()
        except openrouter.OpenRouterError as error:
            self.catalog_error = str(error)
        widget = self.fields["model"].widget
        if isinstance(widget, AIModelWidget):
            widget.models = self.available_models
        if self.catalog_error:
            self.fields["model"].help_text = self.catalog_error
        else:
            self.fields["model"].help_text = gettext_lazy(
                "Type in this field to search available OpenRouter models. Each user's own OpenRouter balance pays for requests."
            )

    def clean_model(self) -> str:
        identifier = str(self.cleaned_data["model"]).strip()
        if self.catalog_error:
            raise forms.ValidationError(
                gettext_lazy(
                    "The model catalog is unavailable. No model changes will be applied; please try again shortly."
                )
            )
        if not any(model.id == identifier for model in self.available_models):
            raise forms.ValidationError(
                gettext_lazy(
                    "Choose a currently available text model from the model search."
                )
            )
        return identifier
