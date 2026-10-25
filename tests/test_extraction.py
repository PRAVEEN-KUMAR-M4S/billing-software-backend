"""The Gemini module: request shape, parsed reply, and failure modes.

No network here — a stand-in client records what we send and replays what the
SDK would hand back.
"""

from types import SimpleNamespace

import pytest
from google.genai import errors

from app.schemas.bill import BillDraft, BillItem
from app.services import extraction

DRAFT = BillDraft(
    shop="Lulu Supermarket",
    date="2026-10-22",
    currency="INR",
    category="groceries",
    total="45.00",
    items=[BillItem(name="Rice 1kg", qty=2, amount="45.00")],
)


class FakeModels:
    """Replay one reply per call; a list lets a test script a model fallback."""

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


@pytest.fixture()
def stub(monkeypatch):
    """Replace the real client with a fake one and pretend a key exists."""

    def install(response):
        monkeypatch.setattr(extraction, "GEMINI_API_KEY", "test-key")
        client = FakeClient(response)
        monkeypatch.setattr(extraction, "_client", client)
        return client

    return install


def test_extract_sends_one_image_and_constrains_the_reply(stub):
    client = stub(SimpleNamespace(parsed=DRAFT, text=None))

    assert extraction.extract_bill(b"photo-bytes", "image/png") == DRAFT

    assert len(client.models.calls) == 1  # one call per bill, always
    call = client.models.calls[0]
    assert call["model"] == extraction.GEMINI_MODEL
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is BillDraft  # our shape, not free-form text
    assert config.system_instruction  # the versioned prompt travels with every call
    image_part = call["contents"][1]
    assert image_part.inline_data.mime_type == "image/png"
    assert image_part.inline_data.data == b"photo-bytes"


def test_prompt_asks_for_rupees_not_paise(stub):
    """The schema is Decimal rupees; the prompt must say the same unit."""
    client = stub(SimpleNamespace(parsed=DRAFT, text=None))
    extraction.extract_bill(b"photo-bytes", "image/png")

    prompt = client.models.calls[0]["config"].system_instruction
    assert "rupees" in prompt
    # The worked example names both units, with rupees as the answer.
    assert "Rs 127.49 is 127.49 (not 12749)" in prompt


def test_prompt_carries_the_category_and_icon_vocabulary(stub):
    client = stub(SimpleNamespace(parsed=DRAFT, text=None))
    extraction.extract_bill(b"photo-bytes", "image/png")

    prompt = client.models.calls[0]["config"].system_instruction
    assert "category" in prompt
    assert "groceries" in prompt
    # Exact hugeicons 1.2.0 member names, or Flutter has nothing to draw.
    assert "strokeRoundedShoppingBasket01" in prompt
    assert "strokeRoundedHome01" in prompt


def test_extract_falls_back_to_json_text_when_nothing_was_parsed(stub):
    # A real reply carries money as the JSON string "45.00" (not a float).
    text = DRAFT.model_dump_json()
    assert '"total":"45.00"' in text
    stub(SimpleNamespace(parsed=None, text=text))
    assert extraction.extract_bill(b"photo-bytes", "image/jpeg") == DRAFT


def test_extract_wraps_api_failures(stub):
    stub(RuntimeError("400 INVALID_ARGUMENT"))
    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.extract_bill(b"photo-bytes", "image/jpeg")
    assert "400" in str(caught.value)
    assert caught.value.status_code == 502


def busy_error():
    return errors.ServerError(
        503,
        {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}},
    )


def quota_error():
    return errors.ClientError(
        429,
        {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}},
    )


def test_a_busy_model_falls_over_to_the_next_model(stub, monkeypatch):
    """503 is the model's capacity, so another model can succeed where a key can't."""
    monkeypatch.setattr(extraction, "GEMINI_FALLBACK_MODELS", ["fallback-model"])
    client = stub([busy_error(), SimpleNamespace(parsed=DRAFT, text=None)])

    assert extraction.extract_bill(b"photo-bytes", "image/png") == DRAFT
    assert [call["model"] for call in client.models.calls] == [
        extraction.GEMINI_MODEL,
        "fallback-model",
    ]


def test_every_model_busy_is_a_503_that_names_them(stub, monkeypatch):
    monkeypatch.setattr(extraction, "GEMINI_FALLBACK_MODELS", ["fallback-model"])
    stub([busy_error(), busy_error()])

    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.extract_bill(b"photo-bytes", "image/png")

    assert caught.value.status_code == 503
    assert extraction.GEMINI_MODEL in str(caught.value)
    assert "fallback-model" in str(caught.value)


def test_spent_quota_is_not_spent_again_on_another_model(stub, monkeypatch):
    monkeypatch.setattr(extraction, "GEMINI_FALLBACK_MODELS", ["fallback-model"])
    client = stub(quota_error())

    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.extract_bill(b"photo-bytes", "image/png")

    assert caught.value.status_code == 429
    assert len(client.models.calls) == 1  # quota is per project: fallback would only burn another bucket
    assert "midnight Pacific" in str(caught.value)


def test_extract_rejects_a_reply_that_is_not_a_bill(stub):
    stub(SimpleNamespace(parsed=None, text="here is your JSON: {oops"))
    with pytest.raises(extraction.ExtractionError):
        extraction.extract_bill(b"photo-bytes", "image/jpeg")


def test_missing_key_fails_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(extraction, "GEMINI_API_KEY", None)
    monkeypatch.setattr(extraction, "_client", None)
    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.extract_bill(b"photo-bytes", "image/jpeg")
    assert "GEMINI_API_KEY" in str(caught.value)
