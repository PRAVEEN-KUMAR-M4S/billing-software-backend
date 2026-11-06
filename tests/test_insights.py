"""Smart Tips: monthly facts, one AI call per month, and a screen that never fails.

The interesting behaviours are not "does the text look nice" (that is the
model's job) but: the facts are exact and only that month's, the call happens
once, a busy model still yields a usable tip, and a fallback gets a second
chance instead of owning the month.
"""

import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.routers import insights as insights_router
from app.schemas.insight import CategoryFact, QuickFacts, ShopFact, TipDraft
from app.services import gemini
from app.services.gemini import GeminiError


def bill(
    shop: str = "Asha Tiffins",
    day: str = "2026-10-05",
    total: str = "350.00",
    category: str = "dining",
) -> dict:
    return {
        "shop": shop,
        "date": day,
        "currency": "INR",
        "category": category,
        "total": total,
        "items": [{"name": "Meal", "qty": 1, "amount": total}],
        "notes": None,
    }


def save(client, headers, **overrides) -> None:
    response = client.post(
        "/bills/confirm", headers=headers, json={"draft": bill(**overrides)}
    )
    assert response.status_code == 201, response.text


def get_insights(client, headers, month: str = "2026-10"):
    return client.get(f"/insights?month={month}", headers=headers)


@pytest.fixture()
def fake_tip(monkeypatch):
    """Stand in for the Gemini call: record it, and answer with a draft or an error."""
    calls = []

    def install(result: TipDraft | Exception):
        def fake_generate(facts, label):
            calls.append((facts.bills, label))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(insights_router.service, "generate_tip", fake_generate)
        return calls

    return install


def test_insights_require_a_token(client):
    assert client.get("/insights").status_code == 401


def test_an_empty_month_explains_itself_without_an_ai_call(client, auth_headers, fake_tip):
    calls = fake_tip(TipDraft(category="dining", headline="unused", action_label="unused"))

    response = get_insights(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2026-10"
    assert body["month_label"] == "October 2026"
    assert body["tip"]["source"] == "local"
    assert body["tip"]["category"] is None  # nothing to filter by yet
    assert body["facts"] == {
        "bills": 0,
        "total": "0.00",
        "average_bill": "0.00",
        "biggest_category": None,
        "most_visited_shop": None,
    }
    assert body["challenge"]["title"]  # the weekly nudge is always there
    assert calls == []  # no bills, no quota spent


def test_the_facts_come_from_that_month_and_that_user_only(client, auth_headers, fake_tip):
    fake_tip(TipDraft(category="dining", headline="x", action_label="x"))
    save(client, auth_headers, total="350.00", category="dining")  # Oct
    save(client, auth_headers, total="100.00", category="groceries", shop="Lulu")  # Oct
    save(client, auth_headers, total="50.00", category="dining")  # Oct
    save(client, auth_headers, total="999.00", category="dining", day="2026-09-20")  # Sept

    facts = get_insights(client, auth_headers).json()["facts"]

    assert facts["bills"] == 3
    assert facts["total"] == "500.00"
    assert facts["average_bill"] == "166.67"  # 500.00 / 3, to the paise
    assert facts["biggest_category"] == {
        "category": "dining",
        "label": "Food & drink",
        "icon": "strokeRoundedRestaurant01",
        "total": "400.00",
        "bills": 2,
    }
    assert facts["most_visited_shop"] == {"shop": "Asha Tiffins", "bills": 2}


def test_the_ai_tip_is_written_once_per_month(client, auth_headers, fake_tip):
    calls = fake_tip(
        TipDraft(
            category="dining",
            headline="  Food & drink is   where most of your money went.  ",
            action_label="See Food & drink bills",
        )
    )
    save(client, auth_headers)

    first = get_insights(client, auth_headers)
    second = get_insights(client, auth_headers)

    assert first.status_code == 200
    tip = first.json()["tip"]
    assert tip["source"] == "ai"
    assert tip["text"] == "Food & drink is where most of your money went."
    assert tip["category"] == "dining"
    assert tip["action_label"] == "See Food & drink bills"
    # One call for the whole month, for that user/month pair.
    assert len(calls) == 1
    assert calls[0] == (1, "October 2026")
    # The cached answer is the same one, including when it was written.
    assert second.json()["tip"] == tip
    assert second.json()["generated_at"] == first.json()["generated_at"]


def test_a_busy_model_still_returns_a_usable_tip(client, auth_headers, fake_tip):
    calls = fake_tip(GeminiError("Gemini is overloaded right now", status_code=503))
    save(client, auth_headers, total="350.00")

    response = get_insights(client, auth_headers)

    assert response.status_code == 200  # never a 5xx because of the model
    tip = response.json()["tip"]
    assert tip["source"] == "local"
    assert "Food & drink" in tip["text"]
    assert "350.00" in tip["text"]  # built from the real numbers, not invented
    assert tip["category"] == "dining"
    assert tip["action_label"] == "See Food & drink bills"
    # The fallback is cached for the retry window, so opening the screen again
    # does not hammer a model that is already struggling.
    assert get_insights(client, auth_headers).status_code == 200
    assert len(calls) == 1


def test_a_fallback_is_retried_after_the_window(
    client, auth_headers, fake_tip, db_file
):
    calls = fake_tip(GeminiError("quota", status_code=429))
    save(client, auth_headers)
    assert get_insights(client, auth_headers).json()["tip"]["source"] == "local"
    assert len(calls) == 1

    # Pretend the fallback was written long before the retry window (no sleeping).
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "UPDATE monthly_insights SET last_attempt_at = '2020-01-01 00:00:00'"
        )

    fake_tip(TipDraft(category="dining", headline="A real tip now.", action_label="See Food & drink bills"))
    upgraded = get_insights(client, auth_headers).json()

    assert upgraded["tip"]["source"] == "ai"
    assert upgraded["tip"]["text"] == "A real tip now."
    assert len(calls) == 2


def test_an_invalid_month_is_rejected(client, auth_headers):
    for month in ("2026-13", "October", "2026-1", "26-10"):
        assert get_insights(client, auth_headers, month).status_code == 422


def test_the_default_month_is_this_month(client, auth_headers):
    body = client.get("/insights", headers=auth_headers).json()
    assert len(body["month"]) == 7 and body["month"][4] == "-"


def test_another_users_bills_are_not_in_my_facts(client, auth_headers, fake_tip):
    calls = fake_tip(TipDraft(category="dining", headline="x", action_label="x"))
    save(client, auth_headers)

    client.post("/auth/register", json={"email": "ben@example.com", "password": "secret123"})
    tokens = client.post(
        "/auth/login", json={"email": "ben@example.com", "password": "secret123"}
    ).json()
    other = {"Authorization": f"Bearer {tokens['access_token']}"}

    body = get_insights(client, other).json()

    assert body["facts"]["bills"] == 0
    assert body["tip"]["source"] == "local"
    assert calls == []  # Ben's empty month never calls the model either


# --- The AI half on its own (no HTTP) --------------------------------------

FACTS = QuickFacts(
    bills=2,
    total=Decimal("400.00"),
    average_bill=Decimal("200.00"),
    biggest_category=CategoryFact(
        category="dining",
        label="Food & drink",
        icon="strokeRoundedRestaurant01",
        total=Decimal("400.00"),
        bills=2,
    ),
    most_visited_shop=ShopFact(shop="Asha Tiffins", bills=2),
)


class FakeModels:
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
def stub_gemini(monkeypatch):
    def install(response):
        monkeypatch.setattr(gemini, "GEMINI_API_KEY", "test-key")
        client = FakeClient(response)
        monkeypatch.setattr(gemini, "_client", client)
        return client

    return install


def test_the_tip_request_sends_the_facts_and_constrains_the_reply(stub_gemini):
    draft = TipDraft(category="dining", headline="x", action_label="x")
    client = stub_gemini(SimpleNamespace(parsed=draft, text=None))

    assert insights_router.service.generate_tip(FACTS, "October 2026") == draft

    call = client.models.calls[0]
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is TipDraft  # schema, not "reply in JSON"
    assert "never invent" in config.system_instruction.lower()
    brief = call["contents"][1]
    # Only the real numbers go in, so there is nothing for the model to make up.
    assert "400.00" in brief
    assert "Asha Tiffins" in brief
    assert "Food & drink" in brief


def test_the_tip_generation_falls_over_a_busy_model(stub_gemini, monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_FALLBACK_MODELS", ["fallback-model"])
    busy = errors.ServerError(
        503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}
    )
    draft = TipDraft(category="dining", headline="x", action_label="x")
    client = stub_gemini([busy, SimpleNamespace(parsed=draft, text=None)])

    assert insights_router.service.generate_tip(FACTS, "October 2026") == draft
    assert [call["model"] for call in client.models.calls] == [
        gemini.GEMINI_MODEL,
        "fallback-model",
    ]


def test_every_model_busy_names_them_in_the_error(stub_gemini, monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_FALLBACK_MODELS", ["fallback-model"])
    busy = errors.ServerError(
        503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}
    )
    stub_gemini([busy, busy])

    with pytest.raises(GeminiError) as caught:
        insights_router.service.generate_tip(FACTS, "October 2026")

    assert caught.value.status_code == 503
    assert "fallback-model" in str(caught.value)


def test_without_a_key_the_screen_still_works(client, auth_headers, monkeypatch):
    """No key is a setup state, not a broken screen: the tip is written locally."""
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", None)
    monkeypatch.setattr(gemini, "_client", None)
    save(client, auth_headers, total="350.00")

    response = get_insights(client, auth_headers)

    assert response.status_code == 200
    assert response.json()["tip"]["source"] == "local"


def test_money_never_leaks_out_as_paise(client, auth_headers, fake_tip):
    fake_tip(TipDraft(category="health", headline="x", action_label="x"))
    save(client, auth_headers, total="127.49", category="health", shop="VCARE")

    facts = get_insights(client, auth_headers).json()["facts"]
    assert facts["total"] == "127.49"
    assert facts["average_bill"] == "127.49"
