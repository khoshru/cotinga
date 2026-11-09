from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import translation

from accounts.models import User
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.models import (
    DEFAULT_AI_MODEL,
    AIModelDefaults,
    MealEstimate,
    OpenRouterSettings,
    default_ai_model,
)

pytestmark = pytest.mark.django_db
MODEL = openrouter.ModelInfo(
    "openai/admin-selected-model",
    "Admin selected model",
    Decimal("0.000001"),
    Decimal("0.000002"),
)
URL = "/admin/tracker/aimodeldefaults/1/change/"


@pytest.fixture(autouse=True)
def catalog() -> Any:
    translation.deactivate_all()
    AIModelDefaults.objects.get_or_create(pk=1)
    with patch("tracker.admin_forms.openrouter.models", return_value=[MODEL]):
        yield
    translation.deactivate_all()


@pytest.fixture
def preferences(owner: User) -> OpenRouterSettings:
    return OpenRouterSettings.objects.create(
        user=owner,
        model="existing/personal-choice",
        encrypted_api_key=encrypt_key("fictional-admin-default-test-key"),
    )


def test_admin_screen_is_discoverable_searchable_and_does_not_expose_keys(
    admin_client: Client, preferences: OpenRouterSettings
) -> None:
    assert b"AI model defaults" in admin_client.get("/admin/").content
    listing = admin_client.get("/admin/tracker/aimodeldefaults/")
    assert listing.status_code == 200 and b"/1/change/" in listing.content
    response = admin_client.get(URL)
    body = response.content.decode()
    assert response.status_code == 200
    assert 'list="ai-model-options"' in body and MODEL.id in body and MODEL.name in body
    assert "Switch all existing users" in body
    assert response.context["adminform"].form["apply_to_existing"].value() is True
    assert "fictional-admin-default-test-key" not in body
    assert preferences.encrypted_api_key not in body
    assert 'name="_saveasnew"' not in body and "deletelink" not in body


def test_apply_to_all_overrides_choices_preserves_keys_and_history(
    admin_client: Client, preferences: OpenRouterSettings
) -> None:
    second = User.objects.create_user("another@example.com", "test-password")
    other = OpenRouterSettings.objects.create(user=second, model="different/model")
    original_key = preferences.encrypted_api_key
    history = MealEstimate.objects.create(
        user=preferences.user,
        date=date(2026, 11, 1),
        model=preferences.model,
        original_input="Rice and lentils",
        result={"meals": []},
    )
    response = admin_client.post(
        URL,
        {"model": MODEL.id, "apply_to_existing": "on", "_save": "Save"},
        follow=True,
    )
    assert response.status_code == 200
    assert f"2 existing users were switched to {MODEL.id}" in response.content.decode()
    assert default_ai_model() == MODEL.id
    preferences.refresh_from_db()
    other.refresh_from_db()
    history.refresh_from_db()
    assert preferences.model == other.model == MODEL.id
    assert preferences.encrypted_api_key == original_key and not other.has_key
    assert history.model == "existing/personal-choice"
    assert history.original_input == "Rice and lentils" and history.result == {
        "meals": []
    }
    assert (
        "Applied the AI model to all existing users"
        in LogEntry.objects.latest("pk").change_message
    )


def test_default_only_keeps_existing_choices_and_is_used_for_new_setups(
    admin_client: Client, client: Client, preferences: OpenRouterSettings
) -> None:
    response = admin_client.post(URL, {"model": MODEL.id, "_save": "Save"})
    assert response.status_code == 302 and default_ai_model() == MODEL.id
    preferences.refresh_from_db()
    assert preferences.model == "existing/personal-choice"
    newcomer = User.objects.create_user("newcomer@example.com", "test-password")
    assert not OpenRouterSettings.objects.filter(user=newcomer).exists()
    client.force_login(newcomer)
    assert client.get("/ai/settings/").status_code == 200
    assert OpenRouterSettings.objects.get(user=newcomer).model == MODEL.id
    assert not OpenRouterSettings.objects.get(user=newcomer).has_key


def test_same_default_can_be_reapplied_after_a_user_changes_their_model(
    admin_client: Client, preferences: OpenRouterSettings
) -> None:
    AIModelDefaults.objects.filter(pk=1).update(model=MODEL.id)
    assert (
        admin_client.post(
            URL, {"model": MODEL.id, "apply_to_existing": "on"}
        ).status_code
        == 302
    )
    preferences.refresh_from_db()
    assert preferences.model == MODEL.id
    preferences.model = "another/personal-choice"
    preferences.save(update_fields=["model"])
    assert (
        admin_client.post(
            URL, {"model": MODEL.id, "apply_to_existing": "on"}
        ).status_code
        == 302
    )
    preferences.refresh_from_db()
    assert preferences.model == MODEL.id


@pytest.mark.parametrize(
    "model", ["", "invalid/model", "https://example.com", "x" * 201]
)
def test_invalid_model_cannot_change_default_or_users(
    admin_client: Client, preferences: OpenRouterSettings, model: str
) -> None:
    response = admin_client.post(URL, {"model": model, "apply_to_existing": "on"})
    assert response.status_code == 200 and response.context["adminform"].form.errors
    preferences.refresh_from_db()
    assert default_ai_model() == DEFAULT_AI_MODEL
    assert preferences.model == "existing/personal-choice"
    assert not LogEntry.objects.exists()


def test_catalog_outage_does_not_apply_unverified_model(
    admin_client: Client, preferences: OpenRouterSettings
) -> None:
    with patch(
        "tracker.admin_forms.openrouter.models",
        side_effect=openrouter.OpenRouterError("Catalog unavailable"),
    ):
        response = admin_client.post(
            URL, {"model": MODEL.id, "apply_to_existing": "on"}
        )
    assert (
        response.status_code == 200
        and b"No model changes will be applied" in response.content
    )
    preferences.refresh_from_db()
    assert (
        default_ai_model() == DEFAULT_AI_MODEL
        and preferences.model == "existing/personal-choice"
    )


def test_bulk_change_and_default_save_roll_back_together(
    admin_client: Client, preferences: OpenRouterSettings
) -> None:
    with (
        patch(
            "tracker.admin.OpenRouterSettings.objects.update",
            side_effect=IntegrityError("write failed"),
        ),
        pytest.raises(IntegrityError),
    ):
        admin_client.post(URL, {"model": MODEL.id, "apply_to_existing": "on"})
    assert default_ai_model() == DEFAULT_AI_MODEL
    preferences.refresh_from_db()
    assert preferences.model == "existing/personal-choice"


def test_only_staff_with_change_permission_can_apply_default(
    client: Client, owner: User, preferences: OpenRouterSettings
) -> None:
    assert (
        client.post(URL, {"model": MODEL.id, "apply_to_existing": "on"}).status_code
        == 302
    )
    client.force_login(owner)
    assert (
        client.post(URL, {"model": MODEL.id, "apply_to_existing": "on"}).status_code
        == 302
    )
    owner.is_staff = True
    owner.save(update_fields=["is_staff"])
    owner.user_permissions.add(Permission.objects.get(codename="view_aimodeldefaults"))
    assert client.get(URL).status_code == 200
    assert (
        client.post(URL, {"model": MODEL.id, "apply_to_existing": "on"}).status_code
        == 403
    )
    assert default_ai_model() == DEFAULT_AI_MODEL
    owner.user_permissions.add(
        Permission.objects.get(codename="change_aimodeldefaults")
    )
    assert (
        client.post(URL, {"model": MODEL.id, "apply_to_existing": "on"}).status_code
        == 302
    )
    preferences.refresh_from_db()
    assert preferences.model == MODEL.id


def test_admin_change_requires_csrf(
    admin_user: User, preferences: OpenRouterSettings
) -> None:
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(admin_user)
    secure.get(URL)
    data = {"model": MODEL.id, "apply_to_existing": "on"}
    assert secure.post(URL, data).status_code == 403
    assert default_ai_model() == DEFAULT_AI_MODEL
    data["csrfmiddlewaretoken"] = secure.cookies["csrftoken"].value
    assert secure.post(URL, data).status_code == 302


def test_singleton_cannot_be_duplicated_or_deleted(admin_client: Client) -> None:
    assert admin_client.get("/admin/tracker/aimodeldefaults/add/").status_code == 403
    assert (
        admin_client.post(
            "/admin/tracker/aimodeldefaults/1/delete/", {"post": "yes"}
        ).status_code
        == 403
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AIModelDefaults.objects.create(pk=2)
    assert AIModelDefaults.objects.count() == 1


def test_fallback_default_and_new_values_are_not_cached() -> None:
    assert str(AIModelDefaults.objects.get(pk=1)) == "AI model defaults"
    AIModelDefaults.objects.all().delete()
    assert default_ai_model() == DEFAULT_AI_MODEL
    AIModelDefaults.objects.create(model=MODEL.id)
    assert default_ai_model() == MODEL.id
    AIModelDefaults.objects.filter(pk=1).update(model="another/default")
    assert default_ai_model() == "another/default"


def test_singleton_can_be_restored_through_admin_when_missing(
    admin_client: Client,
) -> None:
    AIModelDefaults.objects.all().delete()
    response = admin_client.post(
        "/admin/tracker/aimodeldefaults/add/",
        {"model": MODEL.id, "apply_to_existing": "on"},
    )
    assert response.status_code == 302 and AIModelDefaults.objects.get().pk == 1
    assert default_ai_model() == MODEL.id
