"""The balance API: per-user money the user sets, and every bill spends down.

The two halves that matter: the number is stored (not invented), it belongs to
one user only, and a confirmed bill does exactly one decrement — no matter what
the balance was before.
"""

import sqlite3

SET_BALANCE = "/account/balance"


def draft_payload(total: str = "125.00") -> dict:
    """One bill whose lines add up to `total`, so every check passes."""
    return {
        "shop": "Lulu Supermarket",
        "date": "2026-10-22",
        "currency": "INR",
        "category": "groceries",
        "total": total,
        "items": [{"name": "Rice 1kg", "qty": 1, "amount": total}],
        "notes": None,
    }


def set_balance(client, headers, amount):
    return client.put(SET_BALANCE, headers=headers, json={"balance": amount})


def confirm(client, headers, total: str = "125.00"):
    return client.post("/bills/confirm", headers=headers, json={"draft": draft_payload(total)})


def headers_for(client, email: str) -> dict:
    """A second account, for the isolation tests."""
    client.post("/auth/register", json={"email": email, "password": "secret123"})
    tokens = client.post(
        "/auth/login", json={"email": email, "password": "secret123"}
    ).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def stored_balance(db_file, email: str = "ana@example.com") -> int | None:
    with sqlite3.connect(db_file) as connection:
        return connection.execute(
            "SELECT balance_minor FROM users WHERE email = ?", (email,)
        ).fetchone()[0]


def test_account_requires_a_token(client):
    assert client.get("/account").status_code == 401
    assert client.put(SET_BALANCE, json={"balance": "5000.00"}).status_code == 401


def test_a_new_account_has_no_balance_and_has_spent_nothing(client, auth_headers):
    response = client.get("/account", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"balance": None, "spent": "0.00", "currency": "INR"}


def test_setting_the_opening_balance_stores_exact_paise(client, auth_headers, db_file):
    response = set_balance(client, auth_headers, "5000.00")

    assert response.status_code == 200
    # Rupees out, two places — never 500000 and never a float.
    assert response.json()["balance"] == "5000.00"
    assert response.json()["spent"] == "0.00"
    assert stored_balance(db_file) == 500_000
    assert client.get("/account", headers=auth_headers).json()["balance"] == "5000.00"


def test_each_confirmed_bill_is_spent_from_the_balance(client, auth_headers):
    set_balance(client, auth_headers, "5000.00")

    assert confirm(client, auth_headers).status_code == 201
    account = client.get("/account", headers=auth_headers).json()
    assert account["balance"] == "4875.00"
    assert account["spent"] == "125.00"

    assert confirm(client, auth_headers, total="1000.00").status_code == 201
    account = client.get("/account", headers=auth_headers).json()
    assert account["balance"] == "3875.00"
    assert account["spent"] == "1125.00"


def test_a_bill_saved_before_any_balance_was_set_leaves_it_unset(client, auth_headers, db_file):
    assert confirm(client, auth_headers).status_code == 201

    account = client.get("/account", headers=auth_headers).json()
    assert account["balance"] is None  # nothing to spend from yet
    assert account["spent"] == "125.00"
    assert stored_balance(db_file) is None

    # Setting it later is the opening balance again, not 5000 − 125.
    assert set_balance(client, auth_headers, "5000.00").json()["balance"] == "5000.00"


def test_the_balance_can_be_corrected_without_losing_the_spent_total(client, auth_headers):
    set_balance(client, auth_headers, "5000.00")
    confirm(client, auth_headers)

    account = set_balance(client, auth_headers, "3000.00").json()

    assert account["balance"] == "3000.00"  # replaced, not decremented
    assert account["spent"] == "125.00"  # the bills are still counted


def test_paise_are_kept_exactly(client, auth_headers):
    set_balance(client, auth_headers, "1000.00")

    assert confirm(client, auth_headers, total="127.49").status_code == 201

    account = client.get("/account", headers=auth_headers).json()
    assert account["balance"] == "872.51"
    assert account["spent"] == "127.49"


def test_a_rejected_bill_does_not_spend_the_balance(client, auth_headers, db_file):
    set_balance(client, auth_headers, "5000.00")

    # 125.00 of items against a stated 99.99: the cashier check refuses it.
    response = client.post(
        "/bills/confirm",
        headers=auth_headers,
        json={"draft": {**draft_payload("125.00"), "total": "99.99"}},
    )

    assert response.status_code == 422
    assert client.get("/account", headers=auth_headers).json()["balance"] == "5000.00"
    assert client.get("/account", headers=auth_headers).json()["spent"] == "0.00"
    assert stored_balance(db_file) == 500_000


def test_a_bill_bigger_than_the_balance_is_still_saved(client, auth_headers):
    set_balance(client, auth_headers, "100.00")

    assert confirm(client, auth_headers).status_code == 201

    account = client.get("/account", headers=auth_headers).json()
    assert account["balance"] == "-25.00"  # overspent is information, not an error
    assert account["spent"] == "125.00"


def test_a_negative_or_unreadable_balance_is_rejected(client, auth_headers, db_file):
    set_balance(client, auth_headers, "5000.00")

    assert set_balance(client, auth_headers, "-1.00").status_code == 422
    assert set_balance(client, auth_headers, "lots").status_code == 422

    # A rejected correction must not have changed anything.
    assert stored_balance(db_file) == 500_000


def test_zero_is_a_real_balance(client, auth_headers):
    assert set_balance(client, auth_headers, "0").json()["balance"] == "0.00"


def test_one_users_balance_and_spending_stay_theirs(client, auth_headers, db_file):
    other = headers_for(client, "ben@example.com")
    set_balance(client, auth_headers, "5000.00")
    confirm(client, auth_headers)

    account = client.get("/account", headers=other).json()

    assert account == {"balance": None, "spent": "0.00", "currency": "INR"}
    assert stored_balance(db_file, "ben@example.com") is None
    assert stored_balance(db_file) == 487_500
