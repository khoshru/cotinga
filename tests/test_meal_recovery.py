import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import pytest
from django.db.models import CharField
from django.utils import translation

from tracker import openrouter
from tracker.models import NUTRIENTS, Meal

DAY = date(2026, 10, 14)
KEY = "fictional-key-for-recovery-tests"
MODEL = openrouter.ModelInfo("google/gemini-4-argon", "Gemini", Decimal(0), Decimal(0))
PROMPT = "breakfast - 5 medium eggs cooked in olive oil, 2 cups milk, ketchup and one pomegranate."
LONG_PORTION = (
    "5 medium eggs (about 220 g edible weight), cooked in 1 tablespoon olive oil; "
    "2 cups whole milk (480 ml); 1 tablespoon ketchup (15 g); "
    "1 medium pomegranate (about 170 g edible seeds)"
)


def breakfast(*, bilingual: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "meals": [
            {
                "kind": "breakfast",
                "time": None,
                "description": "Eggs in olive oil, milk, ketchup and pomegranate",
                "portion": "5 medium eggs; 1 tbsp oil; 2 cups milk; 1 tbsp ketchup; 1 pomegranate",
                "calories": 1000,
                "protein": 49,
                "carbs": 75,
                "fat": 57,
            }
        ],
        "assumptions": ["Assumed 1 tbsp olive oil, whole milk and 1 tbsp ketchup."],
    }
    if bilingual:
        value["meals"][0].update(
            description_fa="تخم‌مرغ با روغن زیتون، شیر، سس کچاپ و انار",
            portion_fa="۵ تخم‌مرغ متوسط؛ ۱ قاشق غذاخوری روغن؛ ۲ لیوان شیر؛ ۱ قاشق سس؛ ۱ انار",
        )
        value["assumptions_fa"] = [
            "۱ قاشق غذاخوری روغن زیتون، شیر پرچرب و ۱ قاشق سس فرض شد."
        ]
    return value


def response(value: Any) -> dict[str, Any]:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(value)}}
        ]
    }


@pytest.mark.parametrize("language", ["en", "fa"])
def test_breakfast_with_verbose_portions_is_corrected_once_in_the_same_model(
    language: str, caplog: pytest.LogCaptureFixture
) -> None:
    valid = breakfast(bilingual=language == "fa")
    verbose = deepcopy(valid)
    verbose["meals"][0]["portion"] = LONG_PORTION
    with (
        translation.override(language),
        patch(
            "tracker.openrouter._request",
            side_effect=[response(verbose), response(valid)],
        ) as request,
    ):
        output = openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    assert request.call_count == 2
    first, correction = (call.kwargs["payload"] for call in request.call_args_list)
    assert len(first["messages"]) == 2 and len(correction["messages"]) == 4
    assert first["model"] == correction["model"] == MODEL.id
    assert PROMPT in correction["messages"][1]["content"]
    assert "meals[0].portion:max_length" in correction["messages"][-1]["content"]
    assert (
        "Keep every stated food and quantity" in correction["messages"][-1]["content"]
    )
    assert (
        "keep the nutrition estimates unchanged"
        in correction["messages"][-1]["content"]
    )
    assert output["meals"][0] == {
        **valid["meals"][0],
        "date": DAY.isoformat(),
        "time": "",
    }
    assert output["assumptions"] == valid["assumptions"]
    assert "portion:max_length" in caplog.text
    assert KEY not in caplog.text and LONG_PORTION not in caplog.text
    assert PROMPT not in caplog.text
    if language == "fa":
        assert output["assumptions_fa"] == valid["assumptions_fa"]
    else:
        assert "assumptions_fa" not in output


@pytest.mark.parametrize("language", ["en", "fa"])
def test_response_schema_carries_storage_and_nutrition_limits(language: str) -> None:
    with translation.override(language):
        schema = openrouter._response_schema(openrouter.MEAL_SCHEMA, meals=True)
    properties = schema["properties"]
    row = properties["meals"]["items"]["properties"]
    assert properties["meals"]["maxItems"] == openrouter.MAX_MEALS
    for name in ("description", "portion"):
        assert (
            row[name]["maxLength"]
            == cast("CharField[str, str]", Meal._meta.get_field(name)).max_length
        )
        if language == "fa":
            assert row[f"{name}_fa"]["maxLength"] == row[name]["maxLength"]
    for name in NUTRIENTS:
        assert row[name]["minimum"] == 0
        assert row[name]["maximum"] == (10000 if name == "calories" else 1000)
    assert properties["assumptions"]["maxItems"] == 20
    assert properties["assumptions"]["items"]["maxLength"] == 500
    if language == "fa":
        assert properties["assumptions_fa"]["maxItems"] == 20


def test_whole_valued_json_numbers_need_no_extra_request() -> None:
    value = breakfast()
    value["meals"][0]["protein"] = 49.0
    original = deepcopy(value)
    with patch("tracker.openrouter._request", return_value=response(value)) as request:
        output = openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    request.assert_called_once()
    assert type(output["meals"][0]["protein"]) is int
    assert output["meals"][0]["protein"] == 49
    assert value == original


@pytest.mark.parametrize(
    "changes,rule",
    [
        ({"kind": "morning"}, "kind:invalid_choice"),
        ({"portion": "یک لیوان شیر"}, "portion:english_original_required"),
        ({"calories": 10001}, "calories:max_value"),
        ({"protein": -1}, "protein:min_value"),
        ({"fat": 57.5}, "fat:integer_required"),
        ({"protein": True}, "protein:integer_required"),
        ({"calories": "1000"}, "calories:integer_required"),
        ({"time": "8:30 am"}, "time:HH_MM_or_null_required"),
        ({"description": "x" * 241}, "description:max_length"),
    ],
)
def test_invalid_outputs_still_fail_after_one_correction(
    changes: dict[str, Any], rule: str
) -> None:
    value = breakfast()
    value["meals"][0].update(changes)
    with (
        translation.override("en"),
        patch("tracker.openrouter._request", return_value=response(value)) as request,
        pytest.raises(openrouter.MealOutputError) as error,
    ):
        openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    assert request.call_count == 2
    assert any(rule in issue for issue in error.value.issues)
    assert "smaller portions" not in str(error.value)
    assert "meal description is still here" in str(error.value)


def test_missing_farsi_translation_is_corrected_without_losing_english() -> None:
    valid = breakfast(bilingual=True)
    incomplete = deepcopy(valid)
    incomplete["meals"][0]["portion_fa"] = ""
    with (
        translation.override("fa"),
        patch(
            "tracker.openrouter._request",
            side_effect=[response(incomplete), response(valid)],
        ) as request,
    ):
        output = openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    assert request.call_count == 2
    assert output["meals"][0]["portion"] == valid["meals"][0]["portion"]
    assert output["meals"][0]["portion_fa"] == valid["meals"][0]["portion_fa"]


def test_completed_but_malformed_json_gets_one_correction() -> None:
    invalid = {
        "choices": [{"finish_reason": "stop", "message": {"content": "bad JSON"}}]
    }
    with patch(
        "tracker.openrouter._request", side_effect=[invalid, response(breakfast())]
    ) as request:
        output = openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    assert request.call_count == 2
    assert output["meals"][0]["kind"] == "breakfast"


@pytest.mark.parametrize(
    "failure",
    [
        openrouter.OpenRouterError("Provider is busy"),
        {"choices": [{"finish_reason": "length"}]},
        {"choices": [{"finish_reason": "content_filter"}]},
        response({"meals": [], "assumptions": []}),
    ],
)
def test_network_failure_refusal_truncation_or_no_food_do_not_trigger_paid_retries(
    failure: Any,
) -> None:
    with (
        patch(
            "tracker.openrouter._request",
            **(
                {"side_effect": failure}
                if isinstance(failure, Exception)
                else {"return_value": failure}
            ),
        ) as request,
        pytest.raises(openrouter.OpenRouterError),
    ):
        openrouter.estimate_day(KEY, MODEL, "No meals today", DAY)
    request.assert_called_once()


def test_correction_timeout_uses_remaining_time_budget() -> None:
    value = breakfast()
    value["meals"][0]["portion"] = LONG_PORTION
    with (
        patch("tracker.openrouter.monotonic", side_effect=[100, 100, 132, 133]),
        patch(
            "tracker.openrouter._request",
            side_effect=[response(value), response(breakfast())],
        ) as request,
    ):
        openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    assert [call.kwargs["timeout"] for call in request.call_args_list] == [35, 12]


def test_late_response_does_not_start_a_correction() -> None:
    value = breakfast()
    value["meals"][0]["portion"] = LONG_PORTION
    with (
        patch("tracker.openrouter.monotonic", side_effect=[100, 100, 142]),
        patch("tracker.openrouter._request", return_value=response(value)) as request,
        pytest.raises(openrouter.MealOutputError),
    ):
        openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    request.assert_called_once()


def test_oversized_invalid_response_is_not_repeated_in_a_correction() -> None:
    value = breakfast()
    value["meals"][0]["portion"] = "x" * (openrouter.MAX_CORRECTION_CONTENT + 1)
    with (
        patch("tracker.openrouter._request", return_value=response(value)) as request,
        pytest.raises(openrouter.MealOutputError),
    ):
        openrouter.estimate_day(KEY, MODEL, PROMPT, DAY)
    request.assert_called_once()
