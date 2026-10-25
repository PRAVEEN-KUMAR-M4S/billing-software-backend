"""The two route behaviours that matter: nothing is stored without a check.

Gemini itself is faked here — these tests are about the pipeline around it
(auth, file rules, the cashier check, and the single door into the database).
"""

import json

import pytest

from app.config import MAX_UPLOAD_BYTES
from app.routers import bills as bills_router
from app.schemas.bill import BillDraft, BillItem

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def draft_payload(**overrides) -> dict:
    data = {
        "shop": "Lulu Supermarket",
        "date": "2026-10-22",
        "currency": "INR",
        "category": "groceries",
        "total": "125.00",
        "items": [
            {"name": "Rice 1kg", "qty": 2, "amount": "80.00"},
            {"name": "Milk", "qty": 1, "amount": "45.00"},
        ],
        "notes": None,
    }
    data.update(overrides)
    return data


def post_photo(client, headers, content=PNG, content_type="image/png"):
    return client.post(
        "/bills/extract",
        headers=headers,
        files={"file": ("bill.png", content, content_type)},
    )


@pytest.fixture()
def fake_gemini(monkeypatch):
    """Record the call, return the given draft, no network."""

    def install(draft: BillDraft):
        calls = []

        def fake_extract(photo_bytes: bytes, mime_type: str) -> BillDraft:
            calls.append((photo_bytes, mime_type))
            return draft

        monkeypatch.setattr(bills_router, "extract_bill", fake_extract)
        return calls

    return install


def test_extract_requires_a_token(client):
    assert post_photo(client, {}).status_code == 401


def test_extract_rejects_non_images(client, auth_headers):
    response = post_photo(client, auth_headers, content=b"not an image", content_type="application/pdf")
    assert response.status_code == 415


def test_extract_rejects_oversized_photos(client, auth_headers):
    response = post_photo(client, auth_headers, content=b"0" * (MAX_UPLOAD_BYTES + 1))
    assert response.status_code == 413


def test_extract_returns_a_clean_draft(client, auth_headers, fake_gemini):
    calls = fake_gemini(
        BillDraft(
            shop="Lulu Supermarket",
            date="2026-10-22",
            currency="INR",
            category="groceries",
            total="125.00",
            items=[
                BillItem(name="Rice 1kg", qty=2, amount="80.00"),
                BillItem(name="Milk", qty=1, amount="45.00"),
            ],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["problems"] == []
    # Rupees, two places, as a string: never 12500 and never the float 125.0.
    assert body["draft"]["total"] == "125.00"
    assert body["draft"]["items"][0]["amount"] == "80.00"
    assert body["draft"]["currency"] == "INR"
    # A category always arrives with a drawable HugeIcons name, even though the
    # model only answered the category.
    assert body["draft"]["category"] == "groceries"
    assert body["draft"]["icon"] == "strokeRoundedShoppingBasket01"
    # The photo reached the service with its real media type, and nothing else.
    assert calls == [(PNG, "image/png")]


def test_extract_flags_a_bad_total_but_still_returns_the_draft(client, auth_headers, fake_gemini):
    fake_gemini(
        BillDraft(
            shop="Lulu Supermarket",
            date="2026-10-22",
            category="groceries",
            total="99.99",
            items=[BillItem(name="Rice 1kg", qty=2, amount="45.00")],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert [problem["field"] for problem in body["problems"]] == ["total"]
    assert body["draft"]["total"] == "99.99"  # the user still sees what was read


def test_extract_returns_the_pharmacy_bill_the_way_it_is_printed(client, auth_headers, fake_gemini):
    """The reported bill: MRP 15.00 x 10, 15% off + 5% GST → 127.49, paid 128.00.

    The line amount is 127.49 and the cashier rounded it up by 0.51 with no
    Round Off line printed, so the draft passes untouched.
    """
    fake_gemini(
        BillDraft(
            shop="VCAREOTTAPALAM MEDICALS",
            date="2026-10-21",
            category="health",
            total="128.00",
            items=[BillItem(name="NONURD LC TAB", qty=10, amount="127.49")],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["problems"] == []
    assert body["draft"]["items"][0]["amount"] == "127.49"
    assert body["draft"]["total"] == "128.00"


def test_extract_blames_the_total_when_the_mrp_was_read_as_the_line_money(
    client, auth_headers, fake_gemini
):
    """The MRP read as the line money: the rows are 15.00 against a 128.00 bill."""
    fake_gemini(
        BillDraft(
            shop="VCAREOTTAPALAM MEDICALS",
            date="2026-10-21",
            category="health",
            total="128.00",
            items=[BillItem(name="NONURD LC TAB", qty=10, amount="15.00")],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    problems = response.json()["problems"]
    assert [problem["field"] for problem in problems] == ["total"]
    assert "15.00" in problems[0]["message"]


def test_confirm_accepts_a_draft_that_states_its_round_off(client, auth_headers, expenses):
    payload = draft_payload(
        total="127.50",
        round_off="0.50",
        items=[{"name": "Rice 1kg", "qty": 2, "amount": "80.00"},
               {"name": "Milk", "qty": 1, "amount": "47.00"}],
    )
    response = client.post("/bills/confirm", headers=auth_headers, json={"draft": payload})

    assert response.status_code == 201
    assert response.json()["total"] == "127.50"
    assert len(expenses()) == 1


def test_confirm_saves_a_cashier_rounded_bill(client, auth_headers, expenses):
    """127.49 of items, 128.00 paid, no Round Off line on the paper."""
    payload = draft_payload(
        shop="VCAREOTTAPALAM MEDICALS",
        total="128.00",
        items=[{"name": "NONURD LC TAB", "qty": 10, "amount": "127.49"}],
    )
    response = client.post("/bills/confirm", headers=auth_headers, json={"draft": payload})

    assert response.status_code == 201
    assert response.json()["total"] == "128.00"
    assert len(expenses()) == 1


def test_extract_reports_a_gemini_failure_as_502_without_saving(
    client, auth_headers, monkeypatch, expenses
):
    def boom(photo_bytes: bytes, mime_type: str):
        raise bills_router.ExtractionError("Gemini request failed: quota")

    monkeypatch.setattr(bills_router, "extract_bill", boom)

    response = post_photo(client, auth_headers)

    assert response.status_code == 502
    assert "nothing was saved" in response.json()["detail"].lower()
    assert expenses() == []


def test_extract_reports_a_busy_model_as_503_with_retry_after(
    client, auth_headers, monkeypatch, expenses
):
    def busy(photo_bytes: bytes, mime_type: str):
        raise bills_router.ExtractionError(
            "Gemini is overloaded right now (503 UNAVAILABLE — high demand)",
            status_code=503,
        )

    monkeypatch.setattr(bills_router, "extract_bill", busy)

    response = post_photo(client, auth_headers)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert "nothing was saved" in response.json()["detail"].lower()
    assert expenses() == []


def test_extract_reports_spent_quota_as_429(client, auth_headers, monkeypatch, expenses):
    def exhausted(photo_bytes: bytes, mime_type: str):
        raise bills_router.ExtractionError("quota spent", status_code=429)

    monkeypatch.setattr(bills_router, "extract_bill", exhausted)

    response = post_photo(client, auth_headers)

    assert response.status_code == 429
    assert expenses() == []


def test_confirm_refuses_a_wrong_total(client, auth_headers, expenses):
    response = client.post(
        "/bills/confirm",
        headers=auth_headers,
        json={"draft": draft_payload(total="99.99")},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [problem["field"] for problem in detail["problems"]] == ["total"]
    assert "125.00" in detail["problems"][0]["message"]
    assert expenses() == []  # never saved silently


def test_confirm_refuses_unparseable_dates(client, auth_headers):
    response = client.post(
        "/bills/confirm", headers=auth_headers, json={"draft": draft_payload(date="12/03/2026")}
    )
    assert response.status_code == 422
    assert [p["field"] for p in response.json()["detail"]["problems"]] == ["date"]


def test_confirm_saves_the_row_for_the_logged_in_user(client, auth_headers, expenses):
    response = client.post("/bills/confirm", headers=auth_headers, json={"draft": draft_payload()})

    assert response.status_code == 201
    body = response.json()
    assert body["total"] == "125.00"  # rupees on the way out, exact
    assert body["date"] == "2026-10-22"
    assert body["category"] == "groceries"
    assert body["icon"] == "strokeRoundedShoppingBasket01"
    assert body["items"][1]["name"] == "Milk"

    rows = expenses()
    assert len(rows) == 1
    # Paise in SQLite: no floating-point money in the database.
    assert rows[0]["total_minor"] == 12_500
    assert rows[0]["category"] == "groceries"
    assert rows[0]["icon"] == "strokeRoundedShoppingBasket01"
    assert rows[0]["date"] == "2026-10-22"
    assert rows[0]["prompt_version"]  # every saved bill records the prompt that made it
    assert json.loads(rows[0]["items"])[0]["amount"] == "80.00"

    # Two users, two accounts: the row belongs to the token that confirmed it.
    me = client.get("/auth/me", headers=auth_headers).json()
    assert rows[0]["user_id"] == me["id"]


def test_confirm_refuses_a_draft_without_a_category(client, auth_headers, expenses):
    response = client.post(
        "/bills/confirm",
        headers=auth_headers,
        json={"draft": draft_payload(category=None)},
    )

    assert response.status_code == 422
    assert [p["field"] for p in response.json()["detail"]["problems"]] == ["category"]
    assert expenses() == []


def test_confirm_requires_a_token(client):
    response = client.post("/bills/confirm", json={"draft": draft_payload()})
    assert response.status_code == 401
