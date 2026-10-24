"""The cashier check, tested without a server, a photo or Gemini."""

from app.schemas.bill import BillDraft, BillItem
from app.services.checks import check_bill


def good_draft(**overrides) -> BillDraft:
    data = {
        "shop": "Lulu Supermarket",
        "date": "2026-10-22",
        "currency": "INR",
        "total_minor": 12_500,
        "items": [
            BillItem(name="Rice 1kg", qty=2, price_minor=4_500),
            BillItem(name="Milk", qty=1, price_minor=3_500),
        ],
        "notes": None,
    }
    data.update(overrides)
    return BillDraft(**data)


def fields(problems) -> set[str]:
    return {problem.field for problem in problems}


def test_a_correct_bill_has_no_problems():
    assert check_bill(good_draft()) == []


def test_rounding_within_tolerance_is_not_a_problem():
    # Items total 12,500; a printed total of 12,501 is rounding, not an error.
    assert check_bill(good_draft(total_minor=12_501)) == []


def test_wrong_total_is_flagged_with_both_numbers():
    problems = check_bill(good_draft(total_minor=9_999))
    assert fields(problems) == {"total_minor"}
    assert "125.00" in problems[0].message
    assert "99.99" in problems[0].message


def test_ambiguous_date_is_rejected():
    assert fields(check_bill(good_draft(date="12/03/2026"))) == {"date"}
    assert fields(check_bill(good_draft(date="2026-02-30"))) == {"date"}
    assert fields(check_bill(good_draft(date=None))) == {"date"}


def test_foreign_currency_is_flagged():
    assert fields(check_bill(good_draft(currency="USD"))) == {"currency"}
    assert check_bill(good_draft(currency="inr")) == []


def test_unreadable_fields_are_listed_one_by_one():
    draft = good_draft(
        shop=None,
        total_minor=None,
        items=[
            BillItem(name=None, qty=None, price_minor=None),
            BillItem(name="Milk", qty=1, price_minor=3_500),
        ],
    )
    assert fields(check_bill(draft)) == {
        "shop",
        "total_minor",
        "items[0].name",
        "items[0].qty",
        "items[0].price_minor",
    }


def test_no_items_is_flagged():
    assert fields(check_bill(good_draft(items=[]))) == {"items"}


def test_sane_bounds():
    assert fields(check_bill(good_draft(total_minor=0))) == {"total_minor"}
    problems = check_bill(good_draft(items=[BillItem(name="Tea", qty=0, price_minor=-100)]))
    assert {"items[0].qty", "items[0].price_minor"} <= fields(problems)
