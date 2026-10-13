from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client
from django.utils import translation

from accounts.models import User
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.models import (
    DEFAULT_AI_MODEL,
    OpenRouterSettings,
)

MODEL = openrouter.ModelInfo(
    DEFAULT_AI_MODEL,
    "Google: Gemini 4 Argon",
    Decimal("0.000002"),
    Decimal("0.00001"),
    reasoning=True,
)
TARGETS = {
    "calories_min": 1700,
    "calories_max": 1850,
    "protein_min": 110,
    "protein_max": 140,
    "carbs_min": 160,
    "carbs_max": 210,
    "fat_min": 50,
    "fat_max": 65,
}
RESULT = {
    "targets": TARGETS,
    "explanation": "A modest starting range based on your activity.",
    "assumptions": ["Activity is estimated."],
}
PROFILE = {
    "age": 32,
    "height": 180,
    "weight": "85.00",
    "sex": "male",
    "activity": "light",
    "goal": "lose",
    "notes": "Three walks per week.",
}


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    translation.deactivate_all()
    yield
    translation.deactivate_all()


@pytest.fixture
def connected(client: Client, owner: User) -> Client:
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-test-key")
    )
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_language_switch_preserves_page_and_can_switch_back() -> None:
    client = Client(enforce_csrf_checks=True)
    client.get("/accounts/login/")
    assert client.post("/i18n/setlang/", {"language": "fa"}).status_code == 403
    response = client.post(
        "/i18n/setlang/",
        {
            "language": "fa",
            "next": "/accounts/login/?next=/statistics/",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/accounts/login/?next=/statistics/"
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == "fa"
    body = client.get(response.headers["Location"]).content.decode()
    assert '<html lang="fa" dir="rtl">' in body
    assert "خوش برگشتید" in body and "Vazirmatn.woff2" in body
    client.post(
        "/i18n/setlang/",
        {
            "language": "en",
            "next": "/accounts/login/",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert (
        '<html lang="en" dir="ltr">' in client.get("/accounts/login/").content.decode()
    )


@pytest.mark.django_db
def test_default_model_and_inline_picker(client: Client, owner: User) -> None:
    client.force_login(owner)
    with patch("tracker.ai_views.openrouter.models", return_value=[MODEL]):
        body = client.get("/ai/settings/").content.decode()
    assert OpenRouterSettings.objects.get(user=owner).model == DEFAULT_AI_MODEL
    assert 'data-model-input role="combobox"' in body
    assert 'value="google/gemini-4-argon"' in body
    assert "data-model-query" not in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/", "/targets/", "/weight/", "/statistics/", "/ai/targets/"]
)
def test_farsi_pages_render_forms_and_statistics(connected: Client, path: str) -> None:
    connected.cookies[settings.LANGUAGE_COOKIE_NAME] = "fa"
    response = connected.get(path)
    assert response.status_code == 200
    assert '<html lang="fa" dir="rtl">' in response.content.decode()
    assert "خلاصهٔ روز" in response.content.decode()
