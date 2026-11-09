import copy
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from gym import ai
from gym.ai_views import available_exercises, reserve
from gym.models import Exercise, Routine, RoutineExercise, RoutineSuggestion
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.models import DEFAULT_AI_MODEL, OpenRouterSettings

pytestmark = pytest.mark.django_db
MODEL = openrouter.ModelInfo(
    DEFAULT_AI_MODEL,
    "Selected test model",
    Decimal(0),
    Decimal(0),
    temperature=True,
    reasoning=True,
)
INPUT = {
    "description": "Three simple full-body days with dumbbells. No jumping.",
    "days": 3,
    "minutes": 45,
    "experience": "beginner",
    "equipment": ["dumbbell"],
    "include_custom": True,
}


@pytest.fixture(autouse=True)
def setup(client: Client, owner: User) -> Any:
    translation.deactivate_all()
    client.force_login(owner)
    if not Exercise.objects.filter(owner__isnull=True).exists():
        Exercise.objects.bulk_create(
            [
                Exercise(**row)
                for row in import_module(
                    "gym.migrations.0002_exercise_library"
                ).EXERCISES
            ]
        )
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-gym-test-key")
    )
    yield
    translation.deactivate_all()


def result(language: str = "en", days: int = 3) -> dict[str, Any]:
    exercise = Exercise.objects.get(slug="dumbbell-floor-press")
    plan: dict[str, Any] = {
        "summary": "A simple plan with recovery days.",
        "schedule": "Train on nonconsecutive days. Warm up and increase reps gradually.",
        "routines": [
            {
                "name": f"Full body {i + 1}",
                "notes": "Warm up and choose comfortable loads.",
                "estimated_minutes": 30,
                "exercises": [
                    {
                        "exercise_id": exercise.pk,
                        "sets": 3,
                        "reps": 10,
                        "seconds": None,
                        "distance": None,
                        "rest_seconds": 90,
                        "notes": "A simple pressing movement.",
                    }
                ],
            }
            for i in range(days)
        ],
    }
    if language in ("fa", "ka"):
        texts = {
            "fa": [
                "برنامهٔ ساده با روزهای استراحت.",
                "بین تمرین‌ها استراحت کنید.",
                "تمرین تمام بدن",
                "گرم کنید.",
                "حرکتی ساده برای سینه.",
            ],
            "ka": [
                "მარტივი გეგმა დასვენების დღეებით.",
                "ვარჯიშებს შორის დაისვენეთ.",
                "მთელი სხეულის ვარჯიში",
                "გააკეთეთ გახურება.",
                "მარტივი მოძრაობა მკერდისთვის.",
            ],
        }[language]
        plan[f"summary_{language}"], plan[f"schedule_{language}"] = texts[:2]
        for i, day in enumerate(plan["routines"]):
            day[f"name_{language}"] = f"{texts[2]} {i + 1}"
            day[f"notes_{language}"] = texts[3]
            day["exercises"][0][f"notes_{language}"] = texts[4]
    return plan


def request_plan(client: Client, **changes: Any) -> Any:
    payload = {**INPUT, "submission": str(uuid.uuid4()), **changes}
    with (
        patch("gym.ai_views.openrouter.find_model", return_value=MODEL),
        patch(
            "gym.ai_views.ai.suggest_plan",
            side_effect=lambda key, model, data, catalog, language: ai.validate_plan(
                result(language, data["days"]), catalog, data, language
            ),
        ),
    ):
        return client.post("/gym/ai/", payload)


def review_payload(batch: RoutineSuggestion, **changes: Any) -> dict[str, Any]:
    payload = {}
    for i, day in enumerate(batch.result["routines"]):
        payload.update(
            {
                f"day-{i}-keep": "on",
                f"day-{i}-name": day["name"],
                f"day-{i}-notes": day["notes"],
                f"moves-{i}-TOTAL_FORMS": len(day["exercises"]),
                f"moves-{i}-INITIAL_FORMS": len(day["exercises"]),
                f"moves-{i}-MIN_NUM_FORMS": 1,
                f"moves-{i}-MAX_NUM_FORMS": 10,
            }
        )
        for j, row in enumerate(day["exercises"]):
            for name in ("sets", "reps", "seconds", "distance", "rest_seconds"):
                value = row[name]
                payload[f"moves-{i}-{j}-{name}"] = value if value is not None else ""
    return {**payload, **changes}


def test_entry_points_and_missing_key(client: Client, owner: User) -> None:
    for page in ("/gym/", "/gym/routines/new/"):
        assert b"/gym/ai/" in client.get(page).content
    assert b"Generate my plan" in client.get("/gym/ai/").content
    OpenRouterSettings.objects.filter(user=owner).update(encrypted_api_key="")
    assert b"Open AI settings" in client.get("/gym/ai/").content
    assert b"data-busy-form" not in client.get("/gym/ai/").content
    with patch("gym.ai_views.ai.suggest_plan") as call:
        response = client.post("/gym/ai/", {**INPUT, "submission": str(uuid.uuid4())})
        assert response.status_code == 200 and response.context["form"].errors
        call.assert_not_called()
    assert not RoutineSuggestion.objects.exists()


def test_generate_preserves_original_and_requires_review(
    client: Client, owner: User
) -> None:
    response = request_plan(client)
    assert response.status_code == 302
    batch = RoutineSuggestion.objects.get(user=owner)
    assert (
        batch.original_input == INPUT
        and batch.status == "ready"
        and batch.model == DEFAULT_AI_MODEL
    )
    assert not Routine.objects.exists()
    page = client.get(response["Location"])
    assert page.status_code == 200 and b"Save selected routines" in page.content
    assert "no-store" in page["Cache-Control"]
    assert (
        client.get("/gym/ai/").context["form"]["description"].value()
        == INPUT["description"]
    )


def test_review_can_edit_skip_and_save_once(client: Client, owner: User) -> None:
    response = request_plan(client)
    batch = RoutineSuggestion.objects.get(user=owner)
    payload = review_payload(
        batch,
        **{
            "day-0-name": "My edited routine",
            "moves-0-0-sets": "4",
            "moves-0-0-reps": "12",
        },
    )
    payload.pop("day-2-keep")
    assert client.post(response["Location"], payload).status_code == 302
    assert Routine.objects.count() == 2
    routine = Routine.objects.get(name="My edited routine")
    assert routine.ai_suggestion_id == batch.pk
    row = routine.exercises.get()
    assert row.sets == 4 and row.reps == 12 and row.weight is None
    batch.refresh_from_db()
    assert batch.status == "saved" and batch.saved_at
    original = copy.deepcopy(batch.original_input)
    assert client.post(response["Location"], payload).status_code == 200
    assert Routine.objects.count() == 2 and batch.original_input == original
    assert b"My edited routine" in client.get(response["Location"]).content


@pytest.mark.parametrize(
    "field,value",
    [
        ("moves-0-0-sets", "7"),
        ("moves-0-0-reps", "0"),
        ("moves-0-0-rest_seconds", "2001"),
        ("moves-0-TOTAL_FORMS", "11"),
        ("day-0-name", ""),
    ],
)
def test_review_rejects_bad_values(
    client: Client, owner: User, field: str, value: str
) -> None:
    response = request_plan(client)
    batch = RoutineSuggestion.objects.get(user=owner)
    page = client.post(response["Location"], review_payload(batch, **{field: value}))
    assert page.status_code == 200 and b"errorlist" in page.content
    assert not Routine.objects.exists()
    batch.refresh_from_db()
    assert batch.status == "ready"


def test_no_routines_or_all_exercises_skipped(client: Client, owner: User) -> None:
    response = request_plan(client)
    batch = RoutineSuggestion.objects.get(user=owner)
    payload = review_payload(batch)
    for i in range(3):
        payload.pop(f"day-{i}-keep")
    assert (
        b"Choose at least one routine"
        in client.post(response["Location"], payload).content
    )
    payload = review_payload(batch, **{"moves-0-0-DELETE": "on"})
    assert b"errorlist" in client.post(response["Location"], payload).content
    assert not Routine.objects.exists()


def test_requests_are_idempotent_but_changed_input_is_new(
    client: Client, owner: User
) -> None:
    submission = str(uuid.uuid4())
    with (
        patch("gym.ai_views.openrouter.find_model", return_value=MODEL),
        patch(
            "gym.ai_views.ai.suggest_plan", side_effect=lambda *args: result()
        ) as call,
    ):
        first = client.post("/gym/ai/", {**INPUT, "submission": submission})
        second = client.post("/gym/ai/", {**INPUT, "submission": submission})
        assert first["Location"] == second["Location"] and call.call_count == 1
        RoutineSuggestion.objects.update(
            created_at=timezone.now() - timedelta(seconds=30)
        )
        third = client.post(
            "/gym/ai/",
            {**INPUT, "description": "A changed request", "submission": submission},
        )
        assert third["Location"] != first["Location"] and call.call_count == 2


def test_failure_keeps_original_text_and_allows_explicit_retry(
    client: Client, owner: User
) -> None:
    with (
        patch("gym.ai_views.openrouter.find_model", return_value=MODEL),
        patch(
            "gym.ai_views.ai.suggest_plan",
            side_effect=openrouter.OpenRouterError("Provider unavailable"),
        ),
    ):
        response = client.post("/gym/ai/", {**INPUT, "submission": str(uuid.uuid4())})
    assert response.status_code == 200 and b"Provider unavailable" in response.content
    batch = RoutineSuggestion.objects.get(user=owner)
    assert (
        batch.status == "failed"
        and batch.original_input["description"] == INPUT["description"]
    )
    assert response.context["form"]["description"].value() == INPUT["description"]
    assert client.get(f"/gym/ai/{batch.pk}/").status_code == 200


def test_private_views_csrf_and_unavailable_catalog(
    client: Client, owner: User
) -> None:
    response = request_plan(client)
    batch = RoutineSuggestion.objects.get(user=owner)
    other = User.objects.create_user("other@example.com", "test-only")
    client.force_login(other)
    assert client.get(response["Location"]).status_code == 404
    assert client.post(response["Location"], {}).status_code == 404
    client.force_login(owner)
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(owner)
    assert csrf.post(response["Location"], review_payload(batch)).status_code == 403
    Exercise.objects.filter(slug="dumbbell-floor-press").update(archived=True)
    assert client.get(response["Location"]).status_code == 302
    assert not Routine.objects.exists()
    client.logout()
    assert client.get("/gym/ai/").status_code == 302
    assert client.get(response["Location"]).status_code == 302


@pytest.mark.parametrize("language", ["fa", "ka"])
def test_translations_keep_canonical_english(
    client: Client, owner: User, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = request_plan(
        client, description="یک برنامهٔ ساده" if language == "fa" else "მარტივი პროგრამა"
    )
    batch = RoutineSuggestion.objects.get(user=owner)
    assert batch.result["routines"][0]["name"] == "Full body 1"
    assert batch.original_input["description"] != ""
    payload = review_payload(batch)
    for i, day in enumerate(batch.result["routines"]):
        payload[f"day-{i}-name"] = day[f"name_{language}"]
        payload[f"day-{i}-notes"] = day[f"notes_{language}"]
    assert client.get(response["Location"]).status_code == 200
    assert client.post(response["Location"], payload).status_code == 302
    routine = Routine.objects.order_by("pk").first()
    assert routine and routine.name == "Full body 1"
    with translation.override(language):
        assert routine.display_name != routine.name


def test_catalog_constraints_include_fixtures_and_private_ownership(
    owner: User,
) -> None:
    other = User.objects.create_user("foreign@example.com", "test-only")
    mine = Exercise.objects.create(
        owner=owner, name="My curl", muscle="biceps", equipment="dumbbell"
    )
    foreign = Exercise.objects.create(
        owner=other, name="Foreign curl", muscle="biceps", equipment="dumbbell"
    )
    selection = available_exercises(owner, INPUT)
    slugs = {row.slug for row in selection}
    assert "dumbbell-floor-press" in slugs and "dumbbell-bench-press" not in slugs
    assert "pull-up" not in slugs and "dip" not in slugs and "box-jump" not in slugs
    assert mine in selection and foreign not in selection
    expanded = available_exercises(
        owner,
        {
            **INPUT,
            "equipment": ["dumbbell", "bench", "pullup_bar", "dip_bars", "box"],
            "include_custom": False,
        },
    )
    assert mine not in expanded and {
        "pull-up",
        "dip",
        "box-jump",
        "dumbbell-bench-press",
    } <= {row.slug for row in expanded}


def test_structured_payload_is_catalog_constrained(owner: User) -> None:
    catalog = available_exercises(owner, INPUT)
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(result())}}
        ]
    }
    with patch("tracker.openrouter._request", return_value=response) as call:
        plan = ai.suggest_plan("fictional-gym-test-key", MODEL, INPUT, catalog, "en")
    assert plan["routines"]
    payload = call.call_args.kwargs["payload"]
    assert payload["model"] == MODEL.id and payload["provider"]["require_parameters"]
    assert payload["reasoning"]["exclude"] and payload["temperature"] == 0.2
    assert payload["response_format"]["json_schema"]["strict"]
    data = json.loads(payload["messages"][1]["content"])
    assert data["preferences"] == INPUT
    assert {row["exercise_id"] for row in data["allowed_catalog"]} == {
        row.pk for row in catalog
    }
    assert "fictional-gym-test-key" not in json.dumps(payload)
    assert call.call_args.kwargs["timeout"] <= 35
    # Grammar size must not grow with the catalog or expand nested array bounds.
    grammar = json.dumps(payload["response_format"]["json_schema"]["schema"])
    for keyword in ('"enum"', '"minItems"', '"maxItems"', '"exclusiveMinimum"'):
        assert keyword not in grammar


@pytest.mark.parametrize("language", ["en", "fa", "ka"])
def test_argon_schema_rejection_recovers_without_switching_models(
    client: Client, owner: User, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    rejected = Mock(status=400)
    rejected.read.return_value = json.dumps(
        {
            "error": {
                "message": "Provider returned error",
                "metadata": {
                    "raw": "INVALID_ARGUMENT: generation_config.response_schema is too complex; fictional-gym-test-key"
                },
            }
        }
    ).encode()
    accepted = Mock(status=200)
    accepted.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(result(language))},
                }
            ]
        }
    ).encode()
    with (
        translation.override(language),
        patch("tracker.openrouter.find_model", return_value=MODEL),
        patch("tracker.openrouter.HTTPSConnection") as constructor,
    ):
        constructor.return_value.getresponse.side_effect = [rejected, accepted]
        response = client.post("/gym/ai/", {**INPUT, "submission": str(uuid.uuid4())})
    assert response.status_code == 302
    draft = RoutineSuggestion.objects.get(user=owner)
    assert draft.status == RoutineSuggestion.Status.READY
    assert draft.result["routines"] and draft.model == MODEL.id
    assert draft.original_input == INPUT and not Routine.objects.exists()
    payloads = [
        json.loads(call.kwargs["body"])
        for call in constructor.return_value.request.call_args_list
    ]
    assert len(payloads) == 2
    assert [p["response_format"]["type"] for p in payloads] == [
        "json_schema",
        "json_object",
    ]
    for payload in payloads:
        assert payload["model"] == MODEL.id
        assert payload["provider"]["require_parameters"]
        assert "fictional-gym-test-key" not in json.dumps(payload)
    assert "exercise_id" in payloads[1]["messages"][0]["content"]
    if language != "en":
        assert draft.result[f"summary_{language}"]
        assert f"notes_{language}" in payloads[1]["messages"][0]["content"]
    rejected.read.assert_called_once_with(16_385)
    accepted.read.assert_called_once_with(2_000_001)
    assert constructor.return_value.close.call_count == 2
    with translation.override(language):
        saved = client.post(f"/gym/ai/{draft.pk}/", review_payload(draft))
    assert saved.status_code == 302 and Routine.objects.filter(user=owner).count() == 3


@pytest.mark.parametrize(
    "mutation", ["unknown_exercise", "too_many_sets", "wrong_days"]
)
def test_json_compatibility_still_validates_and_shares_two_call_limit(
    owner: User, mutation: str
) -> None:
    value = result()
    if mutation == "unknown_exercise":
        value["routines"][0]["exercises"][0]["exercise_id"] = 999999
    elif mutation == "too_many_sets":
        value["routines"][0]["exercises"][0]["sets"] = 7
    else:
        value["routines"].pop()
    with (
        patch(
            "tracker.openrouter._request",
            side_effect=[
                openrouter.StructuredOutputError("Schema rejected"),
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(value)},
                        }
                    ]
                },
            ],
        ) as call,
        pytest.raises(ai.RoutineOutputError),
    ):
        ai.suggest_plan(
            "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), "en"
        )
    assert call.call_count == 2 and not Routine.objects.exists()


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500])
def test_unrelated_provider_errors_do_not_trigger_gym_retries(
    owner: User, status: int
) -> None:
    with patch("tracker.openrouter.HTTPSConnection") as constructor:
        response = constructor.return_value.getresponse.return_value
        response.status = status
        response.read.return_value = b'{"error":{"message":"Invalid token count"}}'
        with pytest.raises(openrouter.OpenRouterError):
            ai.suggest_plan(
                "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), "en"
            )
    assert constructor.call_count == 1 and not Routine.objects.exists()


def test_schema_fallback_stops_when_request_budget_is_used(owner: User) -> None:
    with (
        patch("gym.ai.monotonic", side_effect=[0, 1, 39]),
        patch(
            "tracker.openrouter.structured_response",
            side_effect=openrouter.StructuredOutputError("Schema rejected"),
        ) as call,
        pytest.raises(ai.RoutineOutputError),
    ):
        ai.suggest_plan(
            "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), "en"
        )
    assert call.call_count == 1


def test_schema_rejection_after_validation_repair_does_not_make_a_third_call(
    owner: User,
) -> None:
    with (
        patch(
            "tracker.openrouter.structured_response",
            side_effect=[
                {"bad": "data"},
                openrouter.StructuredOutputError("Schema rejected"),
            ],
        ) as call,
        pytest.raises(openrouter.StructuredOutputError),
    ):
        ai.suggest_plan(
            "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), "en"
        )
    assert call.call_count == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "duplicate",
        "wrong_days",
        "wrong_mode",
        "too_many_sets",
        "boolean",
        "bad_english",
        "missing_translation",
        "too_long",
        "too_slow",
    ],
)
def test_invalid_model_plans_rejected(owner: User, mutation: str) -> None:
    plan = result()
    row = plan["routines"][0]["exercises"][0]
    if mutation == "unknown":
        row["exercise_id"] = 999999
    elif mutation == "duplicate":
        plan["routines"][0]["exercises"].append(copy.deepcopy(row))
    elif mutation == "wrong_days":
        plan["routines"].pop()
    elif mutation == "wrong_mode":
        row["seconds"] = 10
    elif mutation == "too_many_sets":
        row["sets"] = 20
    elif mutation == "boolean":
        row["reps"] = True
    elif mutation == "bad_english":
        plan["summary"] = "فقط فارسی"
    elif mutation == "missing_translation":
        pass
    elif mutation == "too_long":
        row["notes"] = "x" * 201
    elif mutation == "too_slow":
        row["sets"] = 6
        row["reps"] = 50
        row["rest_seconds"] = 600
    with pytest.raises(ai.RoutineOutputError):
        ai.validate_plan(
            plan,
            available_exercises(owner, INPUT),
            INPUT,
            "fa" if mutation == "missing_translation" else "en",
        )


def test_invalid_plan_gets_one_bounded_correction(owner: User) -> None:
    catalog = available_exercises(owner, INPUT)
    with patch(
        "tracker.openrouter.structured_response",
        side_effect=[{"bad": "data"}, result()],
    ) as call:
        assert ai.suggest_plan("fake-key", MODEL, INPUT, catalog, "en")["routines"]
    assert call.call_count == 2
    with (
        patch(
            "tracker.openrouter.structured_response", return_value={"bad": "data"}
        ) as call,
        pytest.raises(ai.RoutineOutputError),
    ):
        ai.suggest_plan("fake-key", MODEL, INPUT, catalog, "en")
    assert call.call_count == 2


def test_plan_limits_and_processing_status(client: Client, owner: User) -> None:
    token = uuid.uuid4()
    draft, created = reserve(owner, INPUT, token, MODEL.id, "en")
    assert created
    assert reserve(owner, INPUT, token, MODEL.id, "en")[1] is False
    with pytest.raises(openrouter.OpenRouterError):
        reserve(owner, INPUT, uuid.uuid4(), MODEL.id, "en")
    assert client.get(f"/gym/ai/{draft.pk}/").status_code == 200
    RoutineSuggestion.objects.filter(pk=draft.pk).update(
        created_at=timezone.now() - timedelta(minutes=3)
    )
    RoutineSuggestion.objects.bulk_create(
        [
            RoutineSuggestion(user=owner, model=MODEL.id, status="failed")
            for _ in range(19)
        ]
    )
    RoutineSuggestion.objects.update(created_at=timezone.now() - timedelta(minutes=3))
    with pytest.raises(openrouter.OpenRouterError, match="20-plan"):
        reserve(owner, INPUT, uuid.uuid4(), MODEL.id, "en")


@pytest.mark.parametrize("mode", Exercise.Mode.values)
def test_all_tracking_modes_can_be_reviewed_and_saved(
    client: Client, owner: User, mode: str
) -> None:
    exercise = Exercise.objects.create(
        owner=owner,
        name="Custom test movement",
        equipment="bodyweight",
        muscle="core",
        mode=mode,
    )
    plan = result(days=1)
    row = plan["routines"][0]["exercises"][0]
    row.update(
        exercise_id=exercise.pk,
        reps=8
        if mode in ("weight_reps", "bodyweight_reps", "assisted_reps", "reps")
        else None,
        seconds=60 if mode in ("duration", "distance_time") else None,
        distance=1.5 if mode == "distance_time" else None,
    )
    data = {**INPUT, "days": 1}
    prepared = ai.validate_plan(plan, available_exercises(owner, data), data, "en")
    batch = RoutineSuggestion.objects.create(
        user=owner, original_input=data, result=prepared, status="ready", model=MODEL.id
    )
    url = f"/gym/ai/{batch.pk}/"
    assert client.get(url).status_code == 200
    assert client.post(url, review_payload(batch)).status_code == 302
    saved = RoutineExercise.objects.get(routine__ai_suggestion=batch)
    assert saved.exercise_id == exercise.pk and saved.weight is None
    if mode in ("duration", "distance_time"):
        assert saved.seconds == 60
    if mode == "distance_time":
        assert saved.distance == Decimal("1.50")


@pytest.mark.parametrize("language", ["fa", "ka"])
def test_structured_request_asks_for_english_and_interface_language(
    owner: User, language: str
) -> None:
    with patch(
        "tracker.openrouter.structured_response", return_value=result(language)
    ) as call:
        ai.suggest_plan(
            "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), language
        )
    system = call.call_args.kwargs["messages"][0]["content"]
    assert (
        "English" in system
        and ("Persian (Farsi)" if language == "fa" else "Georgian") in system
    )
    schema = call.call_args.kwargs["schema"]
    assert f"summary_{language}" in schema["required"]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"finish_reason": "length"}]},
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "invalid JSON"}}
            ]
        },
        {"choices": [{"finish_reason": "stop", "message": {"content": "[]"}}]},
    ],
)
def test_bad_provider_responses_are_readable_errors(response: dict[str, Any]) -> None:
    with (
        patch("tracker.openrouter._request", return_value=response),
        pytest.raises(openrouter.OpenRouterError, match="could not finish"),
    ):
        openrouter.structured_response(
            "fake-key", MODEL, messages=[], schema={}, name="test"
        )


def test_regeneration_time_budget_and_request_validation(
    client: Client, owner: User
) -> None:
    with (
        patch("gym.ai.monotonic", side_effect=[0, 39]),
        patch("tracker.openrouter.structured_response") as call,
        pytest.raises(ai.RoutineOutputError),
    ):
        ai.suggest_plan(
            "fake-key", MODEL, INPUT, available_exercises(owner, INPUT), "en"
        )
    call.assert_not_called()
    for changes in (
        {"days": 8},
        {"equipment": ["not-real"]},
        {"description": ""},
        {"submission": "bad"},
    ):
        response = client.post(
            "/gym/ai/", {**INPUT, "submission": str(uuid.uuid4()), **changes}
        )
        assert response.status_code == 200 and response.context["form"].errors
    assert not RoutineSuggestion.objects.exists()


def test_seven_day_request_and_source_links(client: Client, owner: User) -> None:
    response = request_plan(client, days=7)
    batch = RoutineSuggestion.objects.get(user=owner)
    assert len(batch.result["routines"]) == 7
    assert client.post(response["Location"], review_payload(batch)).status_code == 302
    routine = Routine.objects.filter(user=owner).first()
    assert (
        routine
        and response["Location"].encode()
        in client.get(f"/gym/routines/{routine.pk}/").content
    )


def test_saved_translation_clears_when_canonical_routine_is_edited(
    client: Client, owner: User
) -> None:
    routine = Routine.objects.create(
        user=owner,
        name="Original",
        name_fa="اصلی",
        notes="Original notes",
        notes_fa="یادداشت",
    )
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/", {"name": "Edited", "notes": "New notes"}
        ).status_code
        == 302
    )
    routine.refresh_from_db()
    assert routine.name == "Edited" and not routine.name_fa and not routine.notes_fa


def test_original_description_preserves_spacing_and_rejects_blank(
    client: Client, owner: User
) -> None:
    description = "  My own description\nwith a second line.  "
    assert request_plan(client, description=description).status_code == 302
    assert (
        RoutineSuggestion.objects.get(user=owner).original_input["description"]
        == description
    )
    response = client.post(
        "/gym/ai/", {**INPUT, "description": "   ", "submission": str(uuid.uuid4())}
    )
    assert response.status_code == 200 and response.context["form"].errors


def test_localized_routine_edit_keeps_english_source(
    client: Client, owner: User
) -> None:
    routine = Routine.objects.create(
        user=owner,
        name="Full body A",
        name_fa="تمام بدن",
        notes="Warm up first.",
        notes_fa="ابتدا گرم کنید.",
    )
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fa"
    page = client.get(f"/gym/routines/{routine.pk}/")
    assert page.context["form"]["name"].value() == "تمام بدن"
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/",
            {"name": "تمرین من", "notes": "آرام شروع کنید."},
        ).status_code
        == 302
    )
    routine.refresh_from_db()
    assert routine.name == "Full body A" and routine.name_fa == "تمرین من"
    assert routine.notes == "Warm up first." and routine.notes_fa == "آرام شروع کنید."


def test_previous_release_can_insert_routines_after_schema_update(owner: User) -> None:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO gym_routine (user_id, name, notes, archived, updated_at) VALUES (%s,%s,%s,%s,%s)",
            [owner.pk, "Older application write", "Preserved", False, timezone.now()],
        )
    routine = Routine.objects.get(name="Older application write")
    assert (
        routine.name_fa == routine.name_ka == routine.notes_fa == routine.notes_ka == ""
    )
    assert routine.ai_suggestion_id is None
