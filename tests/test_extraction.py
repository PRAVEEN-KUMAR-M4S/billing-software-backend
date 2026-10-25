"""The Gemini module: request shape, parsed reply, and failure modes.

No network here — a stand-in client records what we send and replays what the
SDK would hand back.
"""

from types import SimpleNamespace

import pytest

from app.schemas.bill import BillDraft, BillItem
from app.services import extraction

DRAFT = BillDraft(
    shop="Lulu Supermarket",
    date="2026-10-22",
    currency="INR",
    total="45.00",
    items=[BillItem(name="Rice 1kg", qty=2, amount="45.00")],
)


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response):
        self.models = FakeModels(response)


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


def test_extract_falls_back_to_json_text_when_nothing_was_parsed(stub):
    # A real reply carries money as the JSON string "45.00" (not a float).
    text = DRAFT.model_dump_json()
    assert '"total":"45.00"' in text
    stub(SimpleNamespace(parsed=None, text=text))
    assert extraction.extract_bill(b"photo-bytes", "image/jpeg") == DRAFT


def test_extract_wraps_api_failures(stub):
    stub(RuntimeError("503 Service Unavailable"))
    with pytest.raises(extraction.ExtractionError) as caught:
        extraction.extract_bill(b"photo-bytes", "image/jpeg")
    assert "503" in str(caught.value)


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
