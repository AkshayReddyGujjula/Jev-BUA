"""The verdict layer must never pass a page it did not actually check."""

import pytest

from AgentJevUltraFast.assertions import evaluate_expectations, normalize_expect

PAGE = {"url": "https://shop.test/checkout", "title": "Checkout", "text": "Your cart is empty"}


def test_bare_string_is_one_phrase_not_a_list_of_letters():
    # Every letter of "Payment received" occurs somewhere on this page; the phrase does not.
    verified, detail = evaluate_expectations({"text_contains": "Payment received"}, PAGE)
    assert verified is False
    assert detail[0]["check"] == "text_contains"


def test_bare_string_text_absent_is_one_phrase():
    verified, _ = evaluate_expectations({"text_absent": "Payment received"}, PAGE)
    assert verified is True
    verified, _ = evaluate_expectations({"text_absent": "cart is empty"}, PAGE)
    assert verified is False


def test_unknown_key_is_rejected_instead_of_verifying_zero_checks():
    with pytest.raises(ValueError, match="unknown expect key"):
        evaluate_expectations({"text_contain": ["anything"]}, PAGE)


@pytest.mark.parametrize(
    "expect",
    [
        {"url_matches": "("},
        {"url_contains": ""},
        {"url_contains": 3},
        {"text_contains": []},
        {"text_contains": ["ok", 7]},
        {"selector_count": {"at_least": 1}},
        {"selector_count": {"selector": "li", "at_least": -1}},
        {"selector_count": {"selector": "li", "at_least": True}},
        {"selector_count": {"selector": "li", "at_least": 0}},
        {"value_contains": {"selector": "#name"}},
        {"value_contains": {"selector": "#name", "text": ""}},
        {"harvest_at_least": 0},
        {"value_contains": "input"},
        {"harvest_at_least": "3"},
        ["text_contains"],
    ],
)
def test_malformed_shapes_fail_fast(expect):
    with pytest.raises(ValueError):
        normalize_expect(expect)


def test_canonical_shape():
    assert normalize_expect(None) is None
    assert normalize_expect({}) is None
    assert normalize_expect({"text_contains": "x", "selector_count": {"selector": "li"}}) == {
        "text_contains": ["x"],
        "selector_count": {"selector": "li", "at_least": 1},
    }


def test_list_semantics_unchanged():
    # "any of" for text_contains is the documented contract (scripts/run_checks.py).
    verified, _ = evaluate_expectations({"text_contains": ["nope", "cart is empty"]}, PAGE)
    assert verified is True
