import json
from datetime import date
from decimal import Decimal
from http.client import HTTPException
from typing import Any
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from tracker import openrouter
from tracker.credentials import CredentialError, decrypt_key, encrypt_key

KEY = "sk-or-v1-testing-key-with-no-live-value"
DAY = date(2026, 10, 12)
MODEL = openrouter.ModelInfo(
    "openai/test-model", "Test model", Decimal("0.000001"), Decimal("0.000002"), True
)


def result() -> dict[str, Any]:
    return {
        "meals": [
            {
                "kind": "breakfast",
                "time": "08:30",
                "description": "Eggs and toast",
                "portion": "2 eggs, 1 slice of toast",
                "calories": 280,
                "protein": 20,
                "carbs": 20,
                "fat": 15,
            }
        ],
        "assumptions": ["Standard toast slice."],
    }


def test_credentials_are_encrypted_and_support_secret_rotation() -> None:
    with override_settings(SECRET_KEY="first-secret", SECRET_KEY_FALLBACKS=[]):
        encrypted = encrypt_key(KEY)
        assert KEY not in encrypted
        assert decrypt_key(encrypted) == KEY
        assert encrypt_key(KEY) != encrypted
    with override_settings(
        SECRET_KEY="second-secret", SECRET_KEY_FALLBACKS=["first-secret"]
    ):
        assert decrypt_key(encrypted) == KEY
    with override_settings(SECRET_KEY=b"second-secret", SECRET_KEY_FALLBACKS=[]):
        assert decrypt_key(encrypt_key(KEY)) == KEY
        with pytest.raises(CredentialError):
            decrypt_key(encrypted)
        with pytest.raises(CredentialError):
            decrypt_key("not-encrypted")


def test_client_uses_a_fixed_https_host_and_authorization_header() -> None:
    with patch("tracker.openrouter.HTTPSConnection") as constructor:
        connection = constructor.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"data":{}}'
        openrouter.validate_key(KEY)
    constructor.assert_called_once_with("openrouter.ai", timeout=10)
    assert connection.request.call_args.args[:2] == ("GET", "/api/v1/key")
    assert (
        connection.request.call_args.kwargs["headers"]["Authorization"]
        == f"Bearer {KEY}"
    )
    connection.close.assert_called_once()


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500, 302])
def test_http_failures_never_echo_provider_response_or_key(status: int) -> None:
    with patch("tracker.openrouter.HTTPSConnection") as constructor:
        response = constructor.return_value.getresponse.return_value
        response.status = status
        response.read.return_value = KEY.encode()
        with pytest.raises(openrouter.OpenRouterError) as error:
            openrouter.validate_key(KEY)
    assert KEY not in str(error.value)
    response.read.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        b"invalid-json",
        b"[]",
        b'{"error":{"message":"provider secret"}}',
        b"x" * 2_000_001,
    ],
)
def test_invalid_and_oversized_responses_are_bounded(body: bytes) -> None:
    with patch("tracker.openrouter.HTTPSConnection") as constructor:
        response = constructor.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = body
        with pytest.raises(openrouter.OpenRouterError):
            openrouter.models()
    response.read.assert_called_once_with(2_000_001)


@pytest.mark.parametrize("error", [OSError("offline"), HTTPException("bad response")])
def test_network_failures_are_safe(error: Exception) -> None:
    with patch("tracker.openrouter.HTTPSConnection") as constructor:
        constructor.return_value.request.side_effect = error
        with pytest.raises(openrouter.OpenRouterError, match="reach OpenRouter"):
            openrouter.validate_key(KEY)
    constructor.return_value.close.assert_called_once()


def model_row(**changes: object) -> dict[str, Any]:
    return {
        "id": "openai/test-model",
        "name": "Test model",
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["structured_outputs", "temperature"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        **changes,
    }


@pytest.fixture(autouse=True)
def clear_model_cache() -> None:
    cache.clear()


def test_catalog_filters_non_text_batch_and_invalid_models_then_caches() -> None:
    rows = [
        model_row(),
        model_row(
            id="free/model",
            name="Free model",
            pricing={"prompt": "0", "completion": "0"},
        ),
        None,
        {},
        model_row(id="https://evil.invalid?key=steal"),
        model_row(id="openai/test-model:batch"),
        model_row(
            architecture={"input_modalities": ["text"], "output_modalities": ["image"]}
        ),
        model_row(supported_parameters=[]),
        model_row(name=9),
        model_row(pricing={}),
        model_row(pricing={"prompt": "NaN", "completion": "0"}),
        model_row(pricing={"prompt": "-1", "completion": "0"}),
        model_row(pricing={"prompt": "bad", "completion": "0"}),
        model_row(pricing=None),
    ]
    with patch("tracker.openrouter._request", return_value={"data": rows}) as request:
        catalog = openrouter.models()
        assert len(catalog) == 2
        assert catalog[0].free and catalog[0].price_label == "Free"
        assert "$1.00 input / $2.00 output" in catalog[1].price_label
        assert openrouter.find_model(MODEL.id) == catalog[1]
        assert openrouter.models() == catalog
        with pytest.raises(openrouter.OpenRouterError, match="available"):
            openrouter.find_model("missing/model")
    request.assert_called_once()


def test_models_with_request_fees_are_not_labeled_free() -> None:
    row = model_row(pricing={"prompt": "0", "completion": "0", "request": "0.005"})
    with patch("tracker.openrouter._request", return_value={"data": [row]}):
        model = openrouter.models()[0]
    assert not model.free
    assert "$0.005/request" in model.price_label


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": "wrong"}])
def test_invalid_catalog_is_reported(payload: dict[str, object]) -> None:
    with (
        patch("tracker.openrouter._request", return_value=payload),
        pytest.raises(openrouter.OpenRouterError),
    ):
        openrouter.models()


def test_estimation_uses_structured_outputs_and_selected_day() -> None:
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(result())}}
        ]
    }
    with patch("tracker.openrouter._request", return_value=response) as request:
        output = openrouter.estimate_day(KEY, MODEL, "Eggs and toast at 08:30", DAY)
    payload = request.call_args.kwargs["payload"]
    assert payload["model"] == MODEL.id
    assert payload["provider"]["require_parameters"] is True
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["temperature"] == 0.2
    assert KEY not in json.dumps(payload)
    assert output["meals"][0]["date"] == DAY.isoformat()
    assert output["meals"][0]["time"] == "08:30"


def test_unknown_meal_times_stay_blank_and_unsupported_temperature_is_omitted() -> None:
    value = result()
    value["meals"][0]["time"] = None
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(value)}}
        ]
    }
    with patch("tracker.openrouter._request", return_value=response) as request:
        output = openrouter.estimate_day(
            KEY,
            openrouter.ModelInfo(MODEL.id, MODEL.name, Decimal(0), Decimal(0)),
            "Eggs",
            DAY,
        )
    assert "temperature" not in request.call_args.kwargs["payload"]
    assert output["meals"][0]["time"] == ""


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [1]},
        {"choices": [{"finish_reason": "length"}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]},
    ],
)
def test_incomplete_model_outputs_are_rejected(response: dict[str, object]) -> None:
    with (
        patch("tracker.openrouter._request", return_value=response),
        pytest.raises(openrouter.OpenRouterError),
    ):
        openrouter.estimate_day(KEY, MODEL, "Eggs", DAY)


@pytest.mark.parametrize(
    "changes",
    [
        {"calories": True},
        {"protein": -1},
        {"carbs": 1001},
        {"time": "25:00"},
        {"time": "08:30+04:00"},
        {"description": 3},
        {"portion": None},
        {"kind": "unknown"},
        {"description": "x" * 241},
        {"calories": 10001},
    ],
)
def test_invalid_meals_are_rejected(changes: dict[str, object]) -> None:
    value = result()
    value["meals"][0].update(changes)
    with pytest.raises(openrouter.OpenRouterError):
        openrouter.validate_estimate(value, DAY)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"meals": []},
        {"meals": [None]},
        {"meals": [result()["meals"][0]] * 21},
        {**result(), "assumptions": "wrong"},
        {**result(), "assumptions": ["x" * 501]},
        {**result(), "assumptions": [1]},
    ],
)
def test_missing_meals_and_invalid_notes_are_rejected(value: object) -> None:
    with pytest.raises(openrouter.OpenRouterError):
        openrouter.validate_estimate(value, DAY)
