import copy
import json
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from xml.etree import ElementTree

import pytest
from django import forms
from django.apps import apps
from django.conf import settings
from django.test import Client
from django.utils import translation

from accounts.models import User
from gym import ai
from gym.ai_forms import RoutineRequestForm
from gym.ai_views import available_exercises
from gym.catalog import equipment_requirements, movement_details
from gym.models import Exercise, Routine, RoutineExercise, RoutineSuggestion
from tracker.models import DEFAULT_AI_MODEL
from tracker.openrouter import ModelInfo

pytestmark = pytest.mark.django_db
HOME = import_module("gym.migrations.0006_home_exercises")
REQUEST: dict[str, Any] = {
    "description": "Home upper-middle back strength. No equipment.",
    "equipment": [],
    "include_custom": False,
    "days": 3,
    "minutes": 60,
    "experience": "beginner",
}


@pytest.fixture(autouse=True)
def catalog(client: Client, owner: User) -> None:
    client.force_login(owner)
    for migration in ("0002_exercise_library", "0006_home_exercises"):
        rows = import_module(f"gym.migrations.{migration}").EXERCISES
        Exercise.objects.bulk_create(
            [Exercise(**row) for row in rows], ignore_conflicts=True
        )


def test_home_catalog_has_local_images_and_translated_guidance() -> None:
    assert len(HOME.EXERCISES) == 59
    for row in HOME.EXERCISES:
        e = Exercise.objects.get(slug=row["slug"])
        assert e.name_fa and e.name_ka and e.media
        assert e.details["home"] and e.details["kind"] in {
            "strength",
            "mobility",
            "balance",
        }
        for language in ("en", "fa", "ka"):
            with translation.override(language):
                assert e.display_cue and e.display_name
                if language != "en":
                    assert (
                        e.display_cue != e.details["cue"] and e.display_name != e.name
                    )
        for path in cast(list[str], e.media["images"]):
            image = Path(settings.BASE_DIR) / "static" / path
            assert image.is_file() and image.stat().st_size < 1_000_000
            if image.suffix == ".svg":
                for node in ElementTree.parse(image).iter():
                    assert node.tag.split("}")[-1] not in {
                        "script",
                        "foreignObject",
                        "image",
                        "use",
                    }
                    assert not any(
                        k.lower().startswith("on") or k.endswith("href")
                        for k in node.attrib
                    )
    assert {row["muscle"] for row in HOME.EXERCISES} >= {
        "back",
        "chest",
        "shoulders",
        "glutes",
        "quads",
        "hamstrings",
        "calves",
        "core",
        "forearms",
        "triceps",
    }


@pytest.mark.parametrize("query", ["upper-middle back", "پشت میانی", "ზედა ზურგი"])
def test_find_upper_back_without_hidden_equipment(client: Client, query: str) -> None:
    response = client.get(
        "/gym/exercises/", {"q": query, "setting": "none", "fragment": "1"}
    )
    assert response.status_code == 200
    slugs = {e.slug for e in response.context["page"]}
    assert {"prone-y-raise", "prone-t-raise", "reverse-snow-angel"} <= slugs
    assert "back-extension" not in slugs
    assert all(
        not equipment_requirements(e) and e.equipment == "bodyweight"
        for e in response.context["page"]
    )
    assert "setting=none" in response.context["params"]


def test_home_and_anatomical_filters_keep_pagination_and_picker(
    client: Client, owner: User
) -> None:
    routine = Routine.objects.create(user=owner, name="At home")
    params: dict[str, Any] = {
        "setting": "home",
        "muscle": "upper_back",
        "routine": routine.pk,
    }
    response = client.get("/gym/exercises/", params)
    assert response.status_code == 200
    assert all(
        e.details["focus"] == "upper_back" and e.details["home"]
        for e in response.context["page"]
    )
    assert b"At home" in response.content and b"Add exercise" in response.content
    assert "routine=" in response.context["params"]
    assert (
        client.get("/gym/exercises/?setting=home").context["page"].paginator.count > 150
    )


def test_equipment_free_ai_includes_back_strength_and_rejects_fixtures(
    owner: User,
) -> None:
    catalog = available_exercises(owner, REQUEST)
    slugs = {e.slug for e in catalog}
    assert {
        "prone-y-raise",
        "prone-t-raise",
        "reverse-snow-angel",
        "wall-push-ups",
        "bodyweight-glute-bridge",
        "cat-cow",
    } <= slugs
    assert (
        not {
            "band-pull-apart",
            "step-ups",
            "pull-up",
            "ab-wheel-rollout",
            "nordic-curl",
            "jump-rope",
            "battle-ropes",
            "back-extension",
            "banded-kneeling-hip-thrust",
        }
        & slugs
    )
    bands = available_exercises(owner, {**REQUEST, "equipment": ["band"]})
    assert "band-pull-apart" in {e.slug for e in bands}
    assert "band-face-pull" not in {e.slug for e in bands}
    anchored = available_exercises(
        owner, {**REQUEST, "equipment": ["band", "band_anchor", "box", "bench"]}
    )
    assert {"band-face-pull", "banded-kneeling-hip-thrust", "step-ups"} <= {
        e.slug for e in anchored
    }
    field = cast(forms.MultipleChoiceField, RoutineRequestForm().fields["equipment"])
    assert field.clean(["band_anchor"]) == ["band_anchor"]


def test_ai_receives_movement_context_and_can_decline_unsuitable_plan(
    owner: User,
) -> None:
    data = {
        **REQUEST,
        "description": "I have current upper back pain. Make a plan to fix it.",
    }
    explanation = {
        "summary": "Assessment is needed before choosing activities for current pain.",
        "schedule": "Ask a clinician which movements and activity limits are suitable, then update the request.",
        "routines": [],
    }
    with patch(
        "tracker.openrouter.structured_response", return_value=explanation
    ) as call:
        result = ai.suggest_plan(
            "fictional-test-key",
            ModelInfo(DEFAULT_AI_MODEL, "test", Decimal(0), Decimal(0)),
            data,
            available_exercises(owner, data),
            "en",
        )
    assert result == explanation
    messages = call.call_args.kwargs["messages"]
    assert (
        "current pain" in messages[0]["content"]
        and "routines=[]" in messages[0]["content"]
    )
    assert "maximum, not a quota" in messages[0]["content"]
    rows = json.loads(messages[1]["content"])["allowed_catalog"]
    y_raise = next(r for r in rows if r["name"] == "Prone Y Raise")
    assert (
        y_raise["focus"] == "upper_back"
        and y_raise["kind"] == "strength"
        and y_raise["home_suitable"]
    )
    assert y_raise["coaching_cue"] and not y_raise["requires"]


@pytest.mark.parametrize("language", ["en", "fa", "ka"])
def test_no_suitable_plan_review_has_no_save_and_retains_original(
    client: Client, owner: User, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    result = {
        "summary": "A suitable plan needs more information.",
        "schedule": "Clarify permitted activity first.",
        "routines": [],
    }
    if language != "en":
        result[f"summary_{language}"] = (
            "نیاز به اطلاعات بیشتر" if language == "fa" else "საჭიროა მეტი ინფორმაცია"
        )
        result[f"schedule_{language}"] = (
            "ابتدا محدودیت‌ها را مشخص کنید"
            if language == "fa"
            else "ჯერ დააზუსტეთ შეზღუდვები"
        )
    validated = ai.validate_plan(copy.deepcopy(result), [], REQUEST, language)
    batch = RoutineSuggestion.objects.create(
        user=owner,
        original_input=REQUEST,
        model=DEFAULT_AI_MODEL,
        language=language,
        status="ready",
        result=validated,
    )
    response = client.get(f"/gym/ai/{batch.pk}/")
    assert response.status_code == 200 and not response.context["days"]
    assert (
        b"panel gym-ai-save" not in response.content
        and b"panel gym-plan-unavailable" in response.content
    )
    assert REQUEST["description"] in response.content.decode()
    client.post(f"/gym/ai/{batch.pk}/", {})
    assert not Routine.objects.exists()


def test_home_migration_is_idempotent_and_preserves_used_exercises(owner: User) -> None:
    editor = SimpleNamespace(connection=SimpleNamespace(alias="default"))
    existing = Exercise.objects.get(slug="barbell-bench-press")
    used = Exercise.objects.get(slug="prone-y-raise")
    routine = Routine.objects.create(user=owner, name="Preserve me")
    RoutineExercise.objects.create(routine=routine, exercise=used)
    private = Exercise.objects.create(
        owner=owner, name="Private row", muscle="back", equipment="bodyweight"
    )
    HOME.seed(apps, editor)
    assert Exercise.objects.filter(owner__isnull=True).count() == 332
    HOME.unseed(apps, editor)
    assert (
        Exercise.objects.filter(pk__in=[existing.pk, used.pk, private.pk]).count() == 3
    )
    HOME.seed(apps, editor)
    assert Exercise.objects.filter(owner__isnull=True).count() == 332
    assert RoutineExercise.objects.get(routine=routine).exercise_id == used.pk


def test_private_exercise_cannot_impersonate_home_metadata(owner: User) -> None:
    e = Exercise(
        owner=owner, slug="prone-y-raise", name="Mine", muscle="back", equipment="other"
    )
    assert not e.details and not e.media and not e.display_cue and not e.fixtures
    assert not e.focus_label and not e.kind_label
    assert set(movement_details()) >= {row["slug"] for row in HOME.EXERCISES}
