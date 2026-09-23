"""A read-only decision call is hedged once, on its own connection, when it runs long."""

import threading
import time

import pytest

from AgentJevUltraFast import model


def _fake(monkeypatch, primary, hedge):
    """primary/hedge: (delay_s, result or exception). Records which clients were used."""
    calls = []
    lock = threading.Lock()

    def post(url, key, body, timeout=None, client=None, **_):
        which = "hedge" if client is model.HEDGE_CLIENT else "primary"
        with lock:
            calls.append(which)
        delay, outcome = primary if which == "primary" else hedge
        time.sleep(min(delay, timeout))
        if delay > timeout:
            raise RuntimeError("Model call exceeded its budget; no action executed.")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(model, "post_json", post)
    return calls


def test_a_fast_answer_sends_no_hedge(monkeypatch):
    calls = _fake(monkeypatch, (0.0, {"a": 1}), (0.0, {"a": 2}))
    result, hedged = model.post_hedged("u", "k", {}, timeout=2, hedge_after=0.2)
    assert result == {"a": 1} and hedged is None
    assert calls == ["primary"]


def test_a_stalled_request_is_hedged_on_its_own_connection(monkeypatch):
    calls = _fake(monkeypatch, (5.0, {"a": 1}), (0.02, {"a": 2}))
    started = time.monotonic()
    result, hedged = model.post_hedged("u", "k", {}, timeout=2, hedge_after=0.1)
    assert result == {"a": 2} and hedged == "hedge"
    assert calls == ["primary", "hedge"]
    assert time.monotonic() - started < 0.6      # not the primary's stall


def test_a_primary_that_fails_after_the_hedge_left_does_not_fail_the_call(monkeypatch):
    _fake(monkeypatch, (0.15, RuntimeError("Model connection failed")), (0.3, {"a": 2}))
    result, hedged = model.post_hedged("u", "k", {}, timeout=2, hedge_after=0.05)
    assert result == {"a": 2} and hedged == "hedge"


def test_the_first_answer_wins_even_when_it_is_the_primary(monkeypatch):
    _fake(monkeypatch, (0.12, {"a": 1}), (1.0, {"a": 2}))
    result, hedged = model.post_hedged("u", "k", {}, timeout=2, hedge_after=0.05)
    assert result == {"a": 1} and hedged == "primary"


def test_both_stalled_is_still_bounded_by_the_total_budget(monkeypatch):
    _fake(monkeypatch, (5.0, {"a": 1}), (5.0, {"a": 2}))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="budget"):
        model.post_hedged("u", "k", {}, timeout=0.5, hedge_after=0.1)
    assert time.monotonic() - started < 0.9


def test_both_failing_reports_the_failure(monkeypatch):
    _fake(monkeypatch, (0.1, RuntimeError("HTTP 500")), (0.05, RuntimeError("HTTP 502")))
    with pytest.raises(RuntimeError, match="HTTP 50"):
        model.post_hedged("u", "k", {}, timeout=2, hedge_after=0.05)


def test_hedging_can_be_disabled(monkeypatch):
    calls = _fake(monkeypatch, (0.2, {"a": 1}), (0.0, {"a": 2}))
    result, hedged = model.post_hedged("u", "k", {}, timeout=2, hedge_after=0)
    assert result == {"a": 1} and hedged is None
    assert calls == ["primary"]
