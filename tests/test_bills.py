"""The two route behaviours that matter: nothing is stored without a check.

Gemini itself is faked here — these tests are about the pipeline around it
(auth, file rules, the cashier check, and the single door into the database).
"""

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
        "total_minor": 12_500,
        "items": [
            {"name": "Rice 1kg", "qty": 2, "price_minor": 4_500},
            {"name": "Milk", "qty": 1, "price_minor": 3_500},
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
            total_minor=12_500,
            items=[
                BillItem(name="Rice 1kg", qty=2, price_minor=4_500),
                BillItem(name="Milk", qty=1, price_minor=3_500),
            ],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["problems"] == []
    assert body["draft"]["total_minor"] == 12_500
    assert body["draft"]["currency"] == "INR"
    # The photo reached the service with its real media type, and nothing else.
    assert calls == [(PNG, "image/png")]


def test_extract_flags_a_bad_total_but_still_returns_the_draft(client, auth_headers, fake_gemini):
    fake_gemini(
        BillDraft(
            shop="Lulu Supermarket",
            date="2026-10-22",
            total_minor=9_999,
            items=[BillItem(name="Rice 1kg", qty=2, price_minor=4_500)],
        )
    )

    response = post_photo(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert [problem["field"] for problem in body["problems"]] == ["total_minor"]
    assert body["draft"]["total_minor"] == 9_999  # the user still sees what was read


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


def test_confirm_refuses_a_wrong_total(client, auth_headers, expenses):
    response = client.post(
        "/bills/confirm",
        headers=auth_headers,
        json={"draft": draft_payload(total_minor=9_999)},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [problem["field"] for problem in detail["problems"]] == ["total_minor"]
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
    assert body["total_minor"] == 12_500  # paise, an integer — never 125.0
    assert body["date"] == "2026-10-22"
    assert body["items"][1]["name"] == "Milk"

    rows = expenses()
    assert len(rows) == 1
    assert rows[0]["total_minor"] == 12_500
    assert rows[0]["date"] == "2026-10-22"
    assert rows[0]["prompt_version"]  # every saved bill records the prompt that made it

    # Two users, two accounts: the row belongs to the token that confirmed it.
    me = client.get("/auth/me", headers=auth_headers).json()
    assert rows[0]["user_id"] == me["id"]


def test_confirm_requires_a_token(client):
    response = client.post("/bills/confirm", json={"draft": draft_payload()})
    assert response.status_code == 401
