"""The cashier check, tested without a server, a photo or Gemini."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.bill import BillDraft, BillItem, parse_money
from app.services.checks import check_bill


def good_draft(**overrides) -> BillDraft:
    data = {
        "shop": "Lulu Supermarket",
        "date": "2026-10-22",
        "currency": "INR",
        "category": "groceries",
        "total": "125.00",
        "items": [
            BillItem(name="Rice 1kg", qty=2, amount="80.00"),
            BillItem(name="Milk", qty=1, amount="45.00"),
        ],
        "notes": None,
    }
    data.update(overrides)
    return BillDraft(**data)


def fields(problems) -> set[str]:
    return {problem.field for problem in problems}


def test_a_correct_bill_has_no_problems():
    assert check_bill(good_draft()) == []


def test_money_parses_the_ways_a_model_says_it():
    assert parse_money(128) == Decimal("128.00")
    assert parse_money(128.0) == Decimal("128.00")
    assert parse_money("₹1,234.5") == Decimal("1234.50")
    assert parse_money("Rs. 127.49") == Decimal("127.49")
    assert parse_money("N/A") is None  # junk costs one field, not the whole bill
    assert parse_money(True) is None


def test_money_leaves_as_a_two_place_string_never_a_float():
    body = good_draft(total=128).model_dump(mode="json")
    assert body["total"] == "128.00"
    assert body["items"][0]["amount"] == "80.00"


def test_rounding_within_a_rupee_is_not_a_problem():
    # Items total 125.00; a printed 125.99 is cashier rounding, not an error.
    assert check_bill(good_draft(total="125.99")) == []


def test_a_gap_over_a_rupee_is_a_real_mismatch():
    assert fields(check_bill(good_draft(total="126.01"))) == {"total"}


def test_wrong_total_is_flagged_with_both_numbers_and_the_gap():
    problems = check_bill(good_draft(total="99.99"))
    assert fields(problems) == {"total"}
    assert "125.00" in problems[0].message
    assert "99.99" in problems[0].message
    assert "25.01" in problems[0].message


def test_ambiguous_date_is_rejected():
    assert fields(check_bill(good_draft(date="12/03/2026"))) == {"date"}
    assert fields(check_bill(good_draft(date="2026-02-30"))) == {"date"}
    assert fields(check_bill(good_draft(date=None))) == {"date"}


def test_foreign_currency_is_flagged():
    assert fields(check_bill(good_draft(currency="USD"))) == {"currency"}
    assert check_bill(good_draft(currency="inr")) == []


def test_a_bill_without_a_category_is_flagged():
    assert fields(check_bill(good_draft(category=None))) == {"category"}


def test_the_category_supplies_an_icon_when_the_model_picks_none():
    assert good_draft(category="health").icon == "strokeRoundedMedicine01"


def test_an_icon_from_another_category_falls_back_to_this_categorys_own():
    draft = good_draft(category="health", icon="strokeRoundedHome01")
    assert draft.icon == "strokeRoundedMedicine01"


def test_an_icon_from_the_same_category_is_kept():
    draft = good_draft(category="health", icon="strokeRoundedHospital01")
    assert draft.icon == "strokeRoundedHospital01"


def test_an_icon_that_does_not_exist_in_hugeicons_is_rejected_by_the_schema():
    # The enum is the guarantee: Flutter can always draw what the model returns.
    with pytest.raises(ValidationError):
        good_draft(icon="strokeRoundedMadeUpIcon")


def test_a_category_outside_the_list_is_rejected_by_the_schema():
    with pytest.raises(ValidationError):
        good_draft(category="medicine")


def test_unreadable_fields_are_listed_one_by_one():
    draft = good_draft(
        shop=None,
        total=None,
        items=[
            BillItem(name=None, qty=1, amount=None),
            BillItem(name="Milk", qty=1, amount="35.00"),
        ],
    )
    assert fields(check_bill(draft)) == {
        "shop",
        "total",
        "items[0].name",
        "items[0].amount",
    }


def test_a_missing_quantity_is_not_an_error():
    # qty is information, not arithmetic: a bill that prints none still saves.
    draft = good_draft(items=[BillItem(name="Rice 1kg", amount="125.00")])
    assert check_bill(draft) == []


def test_no_items_is_flagged():
    assert fields(check_bill(good_draft(items=[]))) == {"items"}


def test_a_single_line_bigger_than_the_bill_is_flagged_on_the_row():
    draft = good_draft(
        total="128.00",
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="150.00")],
    )
    problems = check_bill(draft)

    assert fields(problems) == {"items[0].amount"}
    assert "150.00" in problems[0].message
    assert "128.00" in problems[0].message


def test_mrp_read_instead_of_the_line_amount_shows_up_on_the_total():
    # The real bill: MRP 15.00 x 10 tablets, 15% off and 5% GST → 127.49, paid
    # 128.00. Reading the MRP as the line money leaves the items 113 short.
    draft = good_draft(
        shop="VCAREOTTAPALAM MEDICALS",
        total="128.00",
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="15.00")],
    )
    problems = check_bill(draft)

    assert fields(problems) == {"total"}
    assert "15.00" in problems[0].message
    assert "128.00" in problems[0].message


def test_read_correctly_that_bill_has_no_problems():
    # 127.49 rounded up to 128.00 by the cashier, no Round Off line printed.
    draft = good_draft(
        shop="VCAREOTTAPALAM MEDICALS",
        total="128.00",
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="127.49")],
    )
    assert check_bill(draft) == []


def test_printed_round_off_reconciles_the_same_bill():
    draft = good_draft(
        total="128.00",
        round_off="0.51",
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="127.49")],
    )
    assert check_bill(draft) == []


def test_round_off_cannot_cover_a_wrong_total():
    draft = good_draft(
        total="128.00",
        round_off="5.00",  # a round-off that does not add up
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="127.49")],
    )
    problems = check_bill(draft)

    assert fields(problems) == {"total"}
    assert "round-off" in problems[0].message
    assert "132.49" in problems[0].message
    assert "128.00" in problems[0].message


def test_amounts_left_in_paise_do_not_reconcile():
    # The old unit: the model says 12749 for 127.49. The total is still rupees,
    # so the maths has to fail rather than save a 100x bill.
    draft = good_draft(
        total="128.00",
        items=[BillItem(name="NONURD LC TAB", qty=10, amount="12749")],
    )
    problems = check_bill(draft)

    # A line 100x the bill is flagged on the row, and the message names rupees.
    assert fields(problems) == {"items[0].amount"}
    assert "12749.00" in problems[0].message
    assert "128.00" in problems[0].message


def test_sane_bounds():
    assert fields(check_bill(good_draft(total="0"))) == {"total"}
    problems = check_bill(good_draft(items=[BillItem(name="Tea", qty=0, amount="-1.00")]))
    assert {"items[0].qty", "items[0].amount"} <= fields(problems)
