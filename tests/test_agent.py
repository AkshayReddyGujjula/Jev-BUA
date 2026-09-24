"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from AgentJevUltraFast import agent as loop
from AgentJevUltraFast import model
from AgentJevUltraFast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body, **_kwargs):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {
        "operation", "click_target", "type_text_target", "goal_done", "stuck", "irreversible",
    }


def _target_sequences(body):
    """The candidate labels per target head, in the order Jev is shown them.

    The index prefix is stripped on purpose: indices are positional and necessarily differ when
    the same page is enumerated in a different order, so comparing them would test nothing.
    """
    def bare(text):
        return text.split("] ", 1)[1] if "] " in text else text

    return {
        name: [bare(q["criteria"][i]["element"]) for i in q["criteria"]]
        for name, q in body["questions"].items()
        if name.endswith("_target")
    }


def test_candidate_order_does_not_depend_on_enumeration_order(monkeypatch):
    """Jev-style decisions are order-sensitive, so candidate order must be an input we control.

    Enumeration order is whatever the DOM walk happened to produce, so without sorting, the same
    page is presented differently between runs and can reach a different answer. Fails without
    the sort because the forward and reversed action lists produce opposite criteria orders.
    """
    seen = []

    def post(_url, _key, body, **_kwargs):
        ops = list(body["questions"]["operation"]["criteria"])
        answers = {"operation": choice(ops, ops[0])}
        for name, question in body["questions"].items():
            if name.endswith("_target"):
                ids = list(question["criteria"])
                answers[name] = {
                    "choice": ids[0],
                    "confidence": 1.0,
                    "probabilities": {i: float(i == ids[0]) for i in ids},
                }
        seen.append(_target_sequences(body))
        return {"model": "test", "answers": answers}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)

    actions = page()["actions"]
    model.choose({**page(), "actions": list(actions)}, "Find a book", [])
    model.choose({**page(), "actions": list(reversed(actions))}, "Find a book", [])

    assert len(seen) == 2
    assert seen[0] == seen[1], "candidate order changed when the page was enumerated differently"


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body, **_kwargs):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body, **_kwargs):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.values = {}
    a.find = ""
    p = page()
    a.state = {
        "browser": Mock(
            fresh=Mock(return_value=True),
            observe=Mock(return_value=p),
            follow_new_tab=Mock(return_value=False),
        ),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
        "progress_at": time.monotonic(),
        "progress_wall_at": time.monotonic(),
        "stall_rounds": 0,
        "recent_fingerprints": [loop.progress_key(p)],
        "stall": None,
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    assert state["status"] == "blocked" and state["outcome"] == "unknown"
    assert state["uncertain_execution"] is True
    assert "could not be read after the action" in state["blocked_reason"]
    runner.state["browser"].act.assert_called_once()


def test_lost_mutation_reply_blocks_further_actions(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].act.side_effect = RuntimeError("session lost after dispatch")
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "blocked"
    assert state["uncertain_execution"] is True
    assert state["blocked_reason"] == "browser dispatch failed; the action may have run"
    assert state["history"][-1]["error"] == "session lost after dispatch"
    assert state["outcome"] == "unknown"
    runner.state["browser"].observe.assert_not_called()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import AgentJevUltraFast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import AgentJevUltraFast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from datetime import date

    from examples.flights import verify

    day = date(2026, 9, 20)
    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual, day)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual, day)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_seek_and_key_actions_are_first_class():
    actions = page()["actions"] + [
        {"id": "e9", "kind": "seek", "label": "Video position [0-120]", "node": 40, "role": "slider", "value": "3"},
        {"id": "key_fullscreen", "kind": "key", "key": "f", "code": "KeyF", "vk": 70, "label": "Toggle fullscreen (F)"},
    ]
    elements, targets, controls = model.action_space(actions)
    assert targets["SEEK"]["3"]["id"] == "e9"
    assert elements[2]["operations"] == ["SEEK"]
    assert "KEY_FULLSCREEN" in controls


def test_key_action_dispatches_key_down_and_up(monkeypatch):
    import AgentJevUltraFast.browser as browser

    cdp = Mock(return_value={})
    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser.browser_operation({"operation": "act", "session": "s", "action": {
        "id": "key_f", "kind": "key", "key": "f", "code": "KeyF", "vk": 70,
    }})
    assert result == {"executed": "key_f"}
    assert cdp.call_count == 2
    assert cdp.call_args_list[0].args[0] == "Input.dispatchKeyEvent"


def _dead_streak(runner):
    runner.state["history"] = [
        {"step": i, "choice": f"x{i}", "kind": "click", "outcome": "no visible change"} for i in (1, 2)
    ]
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


@pytest.fixture
def consult(monkeypatch):
    """The supervisor switched on, with a scripted verdict and a record of every call."""
    calls = []
    script = {"verdict": None}
    monkeypatch.setattr(loop.supervisor, "settings", lambda: {
        "key": "k", "base": "http://x", "model": "sup", "timeout": 1.0, "limit": 2,
        "reasoning": "none"})

    def fake(page, goal, history, **kwargs):
        calls.append({"page": page, "goal": goal, "history": history, **kwargs})
        raw = script["verdict"]
        verdict = loop.supervisor.validate(raw, page, irreversible=kwargs.get("irreversible"))
        return verdict, {"trigger": kwargs["trigger"], "latency_ms": 5, "model": "sup",
                         "usage": {}, "screenshot": bool(kwargs.get("screenshot")), "error": None}

    monkeypatch.setattr(loop.supervisor, "consult", fake)
    return calls, script


def test_dead_streak_blocks_when_the_supervisor_is_off(runner):
    _dead_streak(runner)
    assert runner.state["status"] == "blocked"
    assert not runner.state.get("supervision")


def test_dead_streak_supervisor_action_executes_through_act(runner, consult, monkeypatch):
    calls, script = consult
    script["verdict"] = {"verdict": "act", "element_id": "e1", "text": "laptops", "reason": "fill it"}
    _dead_streak(runner)
    assert calls[0]["trigger"] == "dead_streak"
    assert runner.state["status"] == "ready"
    monkeypatch.setattr(loop, "choose", lambda *a, **k: pytest.fail("no Jev call for a pending action"))
    runner.state["browser"].act.reset_mock()
    runner.command("predict")
    assert runner.state["decision"]["supervisor"] is True
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    executed = runner.state["browser"].act.call_args
    assert executed.args[0]["id"] == "e1"
    assert "laptops" in executed.args + tuple(executed.kwargs.values())
    assert runner.state["text_calls"][-1]["model"] == "supervisor"


def test_pending_supervisor_action_is_dropped_when_the_page_moved(runner, consult, monkeypatch):
    calls, script = consult
    fp = runner.state["page"]["fingerprint"]
    runner.state["decisions"] = [{"irreversible": 0.1, "fingerprint": fp}]
    script["verdict"] = {"verdict": "act", "element_id": "e2", "reason": "open"}
    _dead_streak(runner)
    assert runner.state["supervisor_decision"]["choice"] == "e2"
    moved = page()
    moved["url"] = "https://example.test/elsewhere"
    moved["fingerprint"] = "moved"
    runner.state["page"] = moved
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e1"))
    runner.command("predict")
    assert not runner.state["decision"].get("supervisor")


def test_committable_supervisor_click_becomes_a_hint_not_an_action(runner, consult, monkeypatch):
    calls, script = consult
    runner.state["decisions"] = [{"irreversible": 0.95}]
    script["verdict"] = {"verdict": "act", "element_id": "e3", "reason": "submit"}
    _dead_streak(runner)
    assert "supervisor_decision" not in runner.state
    assert "Go" in runner.state["guidance"]["subgoal"]


def test_hint_steers_jev_then_expires(runner, consult, monkeypatch):
    calls, script = consult
    script["verdict"] = {"verdict": "hint", "subgoal": "Open the search box first"}
    _dead_streak(runner)
    seen = []
    monkeypatch.setattr(loop, "choose", lambda *a, **k: seen.append(k.get("guidance")) or decision("e1"))
    runner.command("predict")
    assert seen == ["Open the search box first"]
    runner.state["history"] += [{"step": 9, "kind": "click", "outcome": "navigated"}] * loop.GUIDANCE_STEPS
    runner.command("predict")
    assert seen[-1] is None


def test_guidance_rides_the_operation_heads_only(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline")
    body = {}

    def fake_post(url, key, payload, **kwargs):
        body.update(payload)
        raise RuntimeError("stop after capturing the request")

    import AgentJevUltraFast.model as m
    original = m.post_json
    m.post_json = fake_post
    try:
        with pytest.raises(RuntimeError):
            m.choose(page(), "Find a book", [], use_cache=False, guidance="Open the menu")
    finally:
        m.post_json = original
    questions = body["questions"]
    assert questions["operation"]["instructions"]["subgoal"] == "Open the menu"
    assert "subgoal" not in questions["goal_done"]["instructions"]
    assert "subgoal" not in questions["irreversible"]["instructions"]


def test_stall_consults_once_per_limit_then_stops(runner, consult):
    calls, script = consult
    script["verdict"] = {"verdict": "hint", "subgoal": "Scroll to the form"}
    runner.state["progress_at"] = time.monotonic() - 30.0
    state = runner._stop_stalled()
    assert state["status"] == "ready" and calls[-1]["trigger"] == "no_progress_deadline"
    assert not runner.stalled()                     # a fresh window was opened
    runner.state["supervisions"] = 2                # the limit is spent
    runner.state["progress_at"] = time.monotonic() - 30.0
    assert runner._stop_stalled()["status"] == "stalled"
    assert len(calls) == 1


def test_model_blocked_can_be_overruled_by_supervisor_done(runner, consult):
    calls, script = consult
    script["verdict"] = {"verdict": "done", "reason": "results are shown"}
    runner.state["decision"] = {**decision("BLOCKED"), "choice": "BLOCKED", "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert calls[0]["trigger"] == "model_blocked"
    assert runner.state["status"] == "done"
    assert runner.state["outcome"] == "unknown"      # a supervisor DONE is not proof


def test_supervisor_never_sees_private_values_or_their_pixels(runner, consult):
    calls, script = consult
    script["verdict"] = {"verdict": "impossible", "reason": "no"}
    runner.private_values = {"hunter22secret"}
    runner._private_pixels = True
    runner.state["goal"] = "Log in with hunter22secret"
    runner.state["browser"].screenshot = Mock(return_value="img")
    runner._supervise("dead_streak")
    assert "hunter22secret" not in json.dumps(calls[0]["goal"])
    assert calls[0]["screenshot"] is None
    runner.state["browser"].screenshot.assert_not_called()
    assert runner.state["status"] == "blocked"
    assert "impossible" in runner.state["blocked_reason"]


def test_reattach_adopts_a_replaced_target(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.target = "old"
    b.session = "dead"
    b.owns_target = True
    b.owned_targets = ["old"]
    b._known_targets = {"old"}
    b.last_url = "https://example.test/"
    monkeypatch.setattr(b, "_page_targets", lambda: [
        {"targetId": "x", "url": "chrome://newtab"},
        {"targetId": "new", "url": "https://example.test/"},
    ])
    attach = Mock(return_value={"sessionId": "fresh"})
    monkeypatch.setattr(browser, "cdp", attach)
    assert b.reattach() is True
    assert b.target == "new" and b.session == "fresh"
    assert "new" in b.owned_targets and "new" in b._known_targets


def test_reattach_fails_without_a_real_page(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.target = "old"
    b.session = "dead"
    b.owns_target = True
    b.owned_targets = ["old"]
    b._known_targets = {"old"}
    b.last_url = "https://example.test/"
    monkeypatch.setattr(b, "_page_targets", lambda: [{"targetId": "x", "url": "chrome://newtab"}])
    monkeypatch.setattr(browser, "cdp", Mock())
    assert b.reattach() is False


def test_reattach_never_adopts_an_existing_same_url_tab(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.target = "old"
    b.session = "dead"
    b.owns_target = True
    b.owned_targets = ["old"]
    b._known_targets = {"old", "other-user-tab"}
    b.last_url = "https://example.test/"
    monkeypatch.setattr(b, "_page_targets", lambda: [
        {"targetId": "other-user-tab", "url": "https://example.test/"},
    ])
    attach = Mock()
    monkeypatch.setattr(browser, "cdp", attach)
    assert b.reattach() is False
    attach.assert_not_called()


def test_call_never_replays_an_ambiguous_cdp_command(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.session = "dead"
    sessions = []

    def fake_cdp(method, session_id=None, **params):
        sessions.append(session_id)
        if session_id == "dead":
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")
        return {"ok": True}

    monkeypatch.setattr(browser, "cdp", fake_cdp)
    monkeypatch.setattr(b, "reattach", lambda: setattr(b, "session", "fresh") or True)
    with pytest.raises(RuntimeError, match="Session with given id"):
        b.call("Page.navigate", url="https://example.test")
    assert sessions == ["dead"]


def test_run_task_never_retries_an_ambiguous_session_error(monkeypatch):
    import run_task

    calls = {"n": 0}

    def fake_run_once(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")
        return [], None, 0

    monkeypatch.setattr(run_task, "run_once", fake_run_once)
    monkeypatch.setattr(sys, "argv", ["run_task.py", "--goal", "g", "--url", "https://example.test/"])
    assert run_task.main() == 1
    assert calls["n"] == 1


def test_browser_does_not_redispatch_an_ambiguous_action(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    calls = []

    def lost_reply(request):
        calls.append(request)
        raise RuntimeError("Session with given id not found")

    monkeypatch.setattr(browser, "browser_operation", lost_reply)
    reattach = Mock(return_value=True)
    monkeypatch.setattr(b, "reattach", reattach)
    with pytest.raises(RuntimeError, match="Session with given id"):
        b._browser_operation({"operation": "act", "action": {"kind": "click"}})
    assert len(calls) == 1
    reattach.assert_not_called()


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_next_best_switches_to_the_next_operation():
    decision_value = {
        "choice": "e1",
        "operation": "TYPE_TEXT",
        "operation_probabilities": {"TYPE_TEXT": 0.6, "CLICK": 0.4},
        "candidate_map": {"TYPE_TEXT": {"1": "e1"}, "CLICK": {"1": "e2", "2": "e3"}},
        "raw_answers": {
            "type_text_target": {"probabilities": {"1": 1.0}},
            "click_target": {"probabilities": {"1": 0.3, "2": 0.7}},
        },
    }
    assert loop.next_best(decision_value, {"e1"}) == {"choice": "e3", "operation": "CLICK", "target": "2"}


def test_next_best_can_fall_back_to_back():
    decision_value = {
        "choice": "e1",
        "operation_probabilities": {"TYPE_TEXT": 0.5, "BACK": 0.5},
        "candidate_map": {"TYPE_TEXT": {"1": "e1"}, "BACK": {"BACK": "BACK"}},
        "raw_answers": {"type_text_target": {"probabilities": {"1": 1.0}}},
    }
    assert loop.next_best(decision_value, {"e1"}) == {"choice": "BACK", "operation": "BACK", "target": None}


def test_next_best_returns_none_when_everything_is_excluded():
    decision_value = {
        "operation_probabilities": {"TYPE_TEXT": 1.0, "DONE": 0.0},
        "candidate_map": {"TYPE_TEXT": {"1": "e1"}},
        "raw_answers": {"type_text_target": {"probabilities": {"1": 1.0}}},
    }
    assert loop.next_best(decision_value, {"e1", "DONE"}) is None


@pytest.mark.parametrize(
    "before,after,error,expected",
    [
        ({"url": "a"}, {"url": "b"}, None, "navigated"),
        (
            {"url": "a", "title": "x", "text": "", "scroll": {"y": 0}},
            {"url": "a", "title": "x", "text": "", "scroll": {"y": 0}},
            None,
            "no visible change",
        ),
        (
            {"url": "a", "title": "x", "text": "", "scroll": {"y": 0}},
            {"url": "a", "title": "x", "text": "", "scroll": {"y": 100}},
            None,
            "scrolled",
        ),
        ({"url": "a"}, {"url": "a"}, "boom", "action failed"),
    ],
)
def test_action_outcome_classification(before, after, error, expected):
    assert loop.action_outcome(before, after, error) == expected


def test_repeated_no_effect_action_switches_to_next_best(runner, monkeypatch):
    runner.state["history"] = [{"step": 1, "choice": "e1", "kind": "click", "outcome": "no visible change"}]
    decision_value = {
        "choice": "e1",
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probability": 0.6,
        "probabilities": {"e1": 0.6},
        "operation_probabilities": {"TYPE_TEXT": 0.6, "CLICK": 0.4},
        "candidate_map": {"TYPE_TEXT": {"1": "e1"}, "CLICK": {"1": "e2", "2": "e3"}},
        "raw_answers": {
            "type_text_target": {"probabilities": {"1": 1.0}},
            "click_target": {"probabilities": {"1": 0.3, "2": 0.7}},
        },
        "usage": {},
        "latency_ms": 10,
    }
    monkeypatch.setattr(loop, "choose", lambda *args, **kwargs: dict(decision_value))
    runner.command("predict")
    assert runner.state["decision"]["choice"] == "e3"
    assert runner.state["decision"]["recovery"]
    assert runner.state["recoveries"] == 1


def test_repeated_no_effect_action_without_alternative_stops(runner, monkeypatch):
    runner.state["history"] = [{
        "step": 1, "choice": "e2", "kind": "click", "outcome": "no visible change",
        "irreversible": 0.95,
    }]
    proposal = {**decision("e2"), "operation": "CLICK", "candidate_map": {"CLICK": {"1": "e2"}}}
    monkeypatch.setattr(loop, "choose", lambda *args, **kwargs: proposal)
    state = runner.command("predict")
    assert state["status"] == "blocked" and state["uncertain_execution"] is True
    assert "no visible effect twice" in state["blocked_reason"]
    runner.state["browser"].act.assert_not_called()


def test_invalid_model_answer_retries_once_then_blocks(runner, monkeypatch):
    """A malformed TypeSafe answer is re-read once, then ends the run honestly."""
    calls = []

    def bad_choose(*args, **kwargs):
        calls.append(1)
        raise ValueError("Invalid TypeSafe response (chosen id is not the argmax)")

    monkeypatch.setattr(loop, "choose", bad_choose)
    state = runner.command("predict")
    assert len(calls) == 2                       # one live re-reading, not a cached replay
    assert state["status"] == "blocked"
    assert state["outcome"] == "unknown"
    assert "invalid model answer" in (runner.state.get("blocked_reason") or "")
    runner.state["browser"].act.assert_not_called()


def test_invalid_model_answer_recovers_on_the_second_reading(runner, monkeypatch):
    calls = []

    def flaky_choose(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("Invalid TypeSafe response (chosen id is not the argmax)")
        return decision("e1")

    monkeypatch.setattr(loop, "choose", flaky_choose)
    state = runner.command("predict")
    assert len(calls) == 2
    assert state["status"] == "predicted"
    assert state["decision"]["choice"] == "e1"


def test_goal_watcher_stops_the_tick_loop(runner, monkeypatch):
    runner.state["history"] = [{"step": 1, "choice": "e1", "kind": "click", "outcome": "page changed"}]
    monkeypatch.setattr(loop, "choose", lambda *args, **kwargs: {
        "choice": "e1", "operation": "TYPE_TEXT", "target": "1", "confidence": 1.0,
        "probability": 0.6, "probabilities": {}, "operation_probabilities": {}, "candidate_map": {},
        "raw_answers": {}, "usage": {}, "latency_ms": 1, "goal_done": 0.95, "stuck": 0.0,
    })
    runner.command("tick")
    assert runner.state["status"] == "done"
    runner.state["browser"].act.assert_not_called()


def test_goal_watcher_stops_without_acting(runner, monkeypatch):
    runner.state["history"] = [{"step": 1, "choice": "e1", "kind": "click", "outcome": "page changed"}]
    monkeypatch.setattr(loop, "choose", lambda *args, **kwargs: {
        "choice": "e1", "operation": "TYPE_TEXT", "target": "1", "confidence": 1.0,
        "probability": 0.6, "probabilities": {}, "operation_probabilities": {}, "candidate_map": {},
        "raw_answers": {}, "usage": {}, "latency_ms": 1, "goal_done": 0.92, "stuck": 0.0,
    })
    runner.command("predict")
    assert runner.state["status"] == "done" and runner.state["decision"] is None


# --- the anti-loop deadline: the loop must never take another lap on its own -----------


def test_new_page_state_is_progress_and_a_repeat_is_not(runner):
    page = runner.state["page"]
    before = runner.state["progress_at"]
    assert runner.note_progress(page) is False          # seeded in the fixture, so already seen
    assert runner.state["progress_at"] == before        # a repeat must not hold the clock open
    assert runner.state["stall_rounds"] == 1
    unseen = {**page, "url": "https://example.test/a-state-we-have-not-seen"}
    assert runner.note_progress(unseen) is True
    assert runner.state["progress_at"] >= before
    assert runner.state["stall_rounds"] == 0


def test_stall_deadline_stops_the_loop_without_calling_the_model(runner, monkeypatch):
    choose = Mock(side_effect=AssertionError("a stalled run must not ask for another decision"))
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["progress_at"] = time.monotonic() - 30.0
    state = runner.command("predict", {})
    assert state["status"] == "stalled"
    assert state["stall"]["seconds_without_progress"] >= 5.0
    assert state["stall"]["limit_s"] == loop.STALL_LIMIT_S
    choose.assert_not_called()


def test_stall_deadline_outranks_a_pending_mutation(runner):
    """The last gate before browser input: an expired deadline must win over the decision."""
    runner.state["progress_at"] = time.monotonic() - 30.0
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "stalled"
    runner.state["browser"].act.assert_not_called()


def test_an_oscillating_page_cannot_hold_the_clock_open(runner, monkeypatch):
    """Alternating between two states is a loop even though every action changes the page."""
    first = runner.state["page"]
    second = {**first, "fingerprint": "the-other-state"}
    runner.state["recent_fingerprints"] = [first["fingerprint"], second["fingerprint"]]
    runner.state["progress_at"] = time.monotonic() - 30.0
    for _ in range(6):
        runner.note_progress(second)
        runner.note_progress(first)
    assert runner.stalled() is True


def test_stall_status_is_terminal_for_the_run_loop(runner):
    assert "stalled" in loop.TERMINAL_STATUS
    runner.state["status"] = "stalled"
    assert runner.state["status"] in loop.TERMINAL_STATUS


# --- caller-supplied values: skip the 1.2-3.3 s text round trip ------------------------


def test_supplied_value_skips_the_text_model(runner, monkeypatch):
    helper = Mock(side_effect=AssertionError("text model must not be called when a value is supplied"))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.values = {"search": "Python programming language"}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    helper.assert_not_called()
    assert state["history"][-1]["text"] == "[redacted]"
    assert state["text_calls"][-1]["model"] == "supplied"
    assert state["text_calls"][-1]["latency_ms"] == 0


def test_absent_supplied_value_still_uses_the_text_model(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.values = {"some other field": "unused"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1


@pytest.mark.parametrize("value", ["", "   ", "x" * 2001])
def test_supplied_value_is_validated_like_the_model_path(runner, value):
    runner.values = {"search": value}
    with pytest.raises(ValueError, match="1-2000 characters"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


@pytest.mark.parametrize("value", ["half", "", "1e999"])
def test_supplied_seek_value_must_be_a_finite_number(runner, value):
    runner.values = {"volume slider": value}
    action = {"id": "s1", "kind": "seek", "label": "Volume slider", "node": 30}
    with pytest.raises(ValueError):
        runner.supplied_text(action)


def test_supplied_seek_value_accepts_a_number(runner):
    runner.values = {"volume slider": "42"}
    action = {"id": "s1", "kind": "seek", "label": "Volume slider", "node": 30}
    assert runner.supplied_text(action) == "42"


def test_supplied_value_matches_a_label_substring(runner):
    runner.values = {"email": "ada@example.com"}
    assert runner.supplied_text({"kind": "fill", "label": "Email address"}) == "ada@example.com"
    assert runner.supplied_text({"kind": "fill", "label": "Password"}) is None


# --- ambient page churn must not hold the stall clock open -----------------------------


def test_ambient_text_churn_is_not_progress(runner):
    """A ticking clock or rotating ad must not look like forward progress.

    Regression for a measured trap: a page whose only change was a fixed-width counter
    reset the clock on every read, ran 61 decisions with zero progress, and the deadline
    never fired once.
    """
    page = runner.state["page"]
    runner.state["recent_fingerprints"] = [loop.progress_key(page)]
    runner.state["progress_at"] = time.monotonic() - 30.0
    for tick in range(20):
        churned = {**page, "text": f"Elapsed {tick}s", "fingerprint": f"fp-{tick}"}
        assert runner.note_progress(churned) is False
    assert runner.stalled() is True


def test_a_moving_clock_alone_never_reaches_the_round_backstop_by_resetting_it(runner):
    page = runner.state["page"]
    runner.state["recent_fingerprints"] = [loop.progress_key(page)]
    for tick in range(loop.STALL_MAX_ROUNDS * 3):
        runner.note_progress({**page, "text": f"tick {tick}"})
    assert runner.state["stall_rounds"] >= loop.STALL_MAX_ROUNDS
    assert runner.stalled() is True


def test_typed_value_counts_as_progress(runner):
    """Control values are included in the progress key on purpose, so filling a field counts."""
    page = runner.state["page"]
    runner.state["recent_fingerprints"] = [loop.progress_key(page)]
    runner.state["progress_at"] = time.monotonic() - 30.0
    typed = {
        **page,
        "actions": [{**page["actions"][0], "value": "laptops"}] + page["actions"][1:],
    }
    assert runner.note_progress(typed) is True
    assert runner.stalled() is False


def test_navigation_counts_as_progress(runner):
    page = runner.state["page"]
    runner.state["recent_fingerprints"] = [loop.progress_key(page)]
    runner.state["progress_at"] = time.monotonic() - 30.0
    assert runner.note_progress({**page, "url": "https://example.test/results"}) is True
    assert runner.stalled() is False


def test_round_backstop_fires_even_with_a_fresh_clock(runner):
    runner.state["stall_rounds"] = loop.STALL_MAX_ROUNDS
    runner.state["progress_at"] = time.monotonic()
    assert runner.stalled() is True
    runner.state["stall_rounds"] = loop.STALL_MAX_ROUNDS - 1
    assert runner.stalled() is False


def test_stop_stalled_records_which_bound_fired(runner):
    runner.state["stall_rounds"] = loop.STALL_MAX_ROUNDS
    state = runner._stop_stalled()
    assert state["stall"]["reason"] == "repeated decisions with no progress"
    assert state["stall"]["round_limit"] == loop.STALL_MAX_ROUNDS
    runner.state["stall_rounds"] = 0
    runner.state["progress_at"] = time.monotonic() - 30.0
    state = runner._stop_stalled()
    assert state["stall"]["reason"] == "no progress inside the deadline"


# --- scrolling: legitimate work is not a loop ------------------------------------------


def scroll_page(page, extra_labels=()):
    return {
        **page,
        "actions": page["actions"]
        + [{"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}]
        + [
            {"id": f"v{i}", "kind": "click", "label": label, "role": "link", "node": 200 + i}
            for i, label in enumerate(extra_labels)
        ],
    }


def test_a_deep_productive_scan_is_never_cut_off(runner):
    """Scanning a long list keeps finding new items, so it is the task, not a loop.

    A human scrolls a long list for minutes to find one item. An earlier version capped the
    streak at 10 scrolls, which in a long list would have forced a click on whatever came
    next - a fast wrong answer instead of a slow right one.
    """
    runner.state["page"] = scroll_page(runner.state["page"])
    for step in range(40):
        view = scroll_page(runner.state["page"], [f"Video {step}"])
        runner.state["browser"].observe.return_value = view
        runner.state["decision"] = {**decision("scroll_down"), "operation": "SCROLL_DOWN"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        assert runner.state["barren_scrolls"] == 0
    assert runner.state.get("scroll_recoveries") is None      # the guard never fired


def test_scrolling_that_reveals_nothing_is_the_loop(runner):
    """The loop is a scroll that shows nothing new - that is what gets cut off."""
    view = scroll_page(runner.state["page"])
    runner.state["page"] = view
    # A real page always offers the scroll control, so it is already known on the first
    # scroll. Without this the control's own label counted as freshly revealed content.
    runner.state["seen_labels"] = [a["label"] for a in view["actions"] if a.get("label")]
    runner.state["browser"].observe.return_value = view
    for _ in range(loop.BARREN_SCROLL_LIMIT):
        runner.state["decision"] = {**decision("scroll_down"), "operation": "SCROLL_DOWN"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["barren_scrolls"] >= loop.BARREN_SCROLL_LIMIT


def test_a_non_scroll_action_clears_the_barren_streak(runner):
    page = scroll_page(runner.state["page"])
    runner.state["page"] = page
    runner.state["barren_scrolls"] = loop.BARREN_SCROLL_LIMIT - 1
    runner.state["browser"].observe.return_value = page
    runner.state["decision"] = decision("e3")           # a click, not a scroll
    runner.command("act", {"fingerprint": page["fingerprint"]})
    assert runner.state["barren_scrolls"] == 0


# --- v2: code-owned outcomes, risk gates, escape hatches, decision cache ---------------


def test_outcome_precedence():
    from AgentJevUltraFast import outcomes

    assert outcomes.resolve("blocked", checks=[{"check": "x", "pass": True}]) == "verified"
    assert outcomes.resolve("done", checks=[{"check": "x", "pass": False}]) == "refuted"
    assert outcomes.resolve("done") == "unknown"
    assert outcomes.resolve("abstained") == "abstained"
    assert outcomes.resolve("stalled") == "budget_exhausted"
    assert outcomes.resolve("budget_exhausted") == "budget_exhausted"
    assert outcomes.resolve("done", checks=[]) == "unknown"


def test_done_with_a_failing_readback_is_refuted(runner):
    runner.expect = {"text_contains": ["Thank you"]}
    runner.state["browser"].evaluate = Mock(return_value={
        "url": "https://example.test", "title": "Example", "text": "Nothing here",
        "selector_counts": {}, "values": {},
    })
    runner.state["status"] = "done"
    runner._finalize()
    assert runner.state["outcome"] == "refuted"
    assert runner.state["checks"][0]["pass"] is False


def test_a_passing_readback_verifies_even_a_blocked_run(runner):
    """Evidence outranks the agent's own opinion: the page is what it is."""
    runner.expect = {"text_contains": ["Done"]}
    runner.state["browser"].evaluate = Mock(return_value={
        "url": "https://example.test", "title": "Example", "text": "Done",
        "selector_counts": {}, "values": {},
    })
    runner.state["status"] = "blocked"
    runner._finalize()
    assert runner.state["outcome"] == "verified"


def test_a_run_without_an_expectation_is_never_verified(runner):
    runner.state["status"] = "done"
    runner._finalize()
    assert runner.state["outcome"] == "unknown"


def test_harvest_count_alone_is_not_a_live_page_verdict():
    from AgentJevUltraFast.outcomes import resolve

    assert resolve("done", [{"check": "harvest_at_least", "pass": True}]) == "unknown"


def test_failed_live_readback_cannot_verify_a_stale_observation(runner):
    runner.expect = {"url_contains": "example"}
    runner.state["browser"].evaluate = Mock(side_effect=ConnectionError("browser gone"))
    runner.state["status"] = "done"
    runner._finalize()
    assert runner.state["outcome"] == "unknown"
    assert runner.state.get("checks") is None
    assert runner.state["readback_error"] == "ConnectionError"


def test_explicit_budget_stop_is_terminal_and_finalized(runner):
    state = runner.stop_budget("caller time limit")
    assert state["status"] == "budget_exhausted"
    assert state["outcome"] == "budget_exhausted"
    assert state["budget_reason"] == "caller time limit"


@pytest.mark.parametrize("confidence,irreversible,gated", [
    (0.9, 0.0, False),
    (0.7, 0.0, False),
    (0.4, 0.0, True),           # a click labelled "Pay" keeps a floor whatever the noul says
    (0.7, 0.9, True),           # irreversible floor is 0.9
    (0.95, 0.9, False),
])
def test_confidence_gate_floors(confidence, irreversible, gated):
    from AgentJevUltraFast import gates

    verdict = gates.check(
        {"confidence": confidence, "irreversible": irreversible},
        {"kind": "click", "label": "Pay"},
    )
    assert (verdict is not None) is gated
    assert gates.check({"confidence": 0.1, "irreversible": 1.0},
                       {"kind": "scroll", "label": "Scroll down"}) is None


@pytest.fixture
def reversible_floor(monkeypatch):
    """Opt back in to a reversible floor (JEV_GATE_REVERSIBLE=0.5) to test the machinery."""
    from AgentJevUltraFast import gates

    monkeypatch.setattr(gates, "REVERSIBLE_MIN", 0.5)


def test_reversible_floor_is_opt_in(monkeypatch):
    from AgentJevUltraFast import gates

    click = {"kind": "click", "label": "Search", "role": "button"}
    assert gates.check({"confidence": 0.47, "irreversible": 0.1}, click) is None
    monkeypatch.setattr(gates, "REVERSIBLE_MIN", 0.5)
    assert gates.check({"confidence": 0.47, "irreversible": 0.1}, click) is not None


def test_low_confidence_mutation_reobserves_then_skips(runner, reversible_floor):
    runner.state["decision"] = {**decision(), "confidence": 0.2}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "ready"
    assert state["gate"]["resolution"] == "reobserve"
    assert state["skip_cache_once"] is True
    runner.state["browser"].act.assert_not_called()
    runner.state["decision"] = {**decision(), "confidence": 0.2}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "ready"
    assert state["gate"]["resolution"] == "skipped"
    assert state["history"][-1]["error"].startswith("skipped")
    assert state["history"][-1]["outcome"] == "action failed"
    assert state.get("outcome") is None      # a skip is not terminal
    runner.state["browser"].act.assert_not_called()


def test_second_low_confidence_irreversible_reading_abstains(runner):
    runner.state["decision"] = {**decision("e3"), "confidence": 0.7, "irreversible": 0.95}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["gate"]["resolution"] == "reobserve"
    runner.state["decision"] = {**decision("e3"), "confidence": 0.7, "irreversible": 0.95}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "abstained"
    assert state["gate"]["resolution"] == "abstain"
    assert state["outcome"] == "abstained"
    runner.state["browser"].act.assert_not_called()


def test_a_refusal_reveals_content_below_the_fold(runner, reversible_floor):
    page = runner.state["page"]
    page["actions"].append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560})
    runner.state["decision"] = {**decision("e3"), "confidence": 0.2}
    runner.command("act", {"fingerprint": page["fingerprint"]})
    assert runner.state["gate"]["resolution"] == "reobserve"
    runner.state["decision"] = {**decision("e3"), "confidence": 0.2}
    state = runner.command("act", {"fingerprint": page["fingerprint"]})
    assert state["status"] == "ready"
    assert runner.state["history"][-1]["kind"] == "scroll"
    assert "reveal" in runner.state["history"][-1]["recovery"]
    assert runner.state["browser"].act.call_count == 1


def test_a_skipped_choice_is_routed_around(runner, monkeypatch):
    """A refused choice must not come back on the same page: the loop moves to the next-best."""
    runner.state["skipped"] = {runner.state["page"]["fingerprint"]: ["e1"]}
    decision_value = {
        "choice": "e1",
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 0.3,
        "probability": 0.3,
        "probabilities": {},
        "operation_probabilities": {"TYPE_TEXT": 0.5, "CLICK": 0.5},
        "candidate_map": {"TYPE_TEXT": {"1": "e1"}, "CLICK": {"1": "e2", "2": "e3"}},
        "raw_answers": {
            "type_text_target": {"probabilities": {"1": 1.0}},
            "click_target": {"probabilities": {"1": 0.3, "2": 0.7}},
        },
        "usage": {}, "latency_ms": 1, "goal_done": 0.0, "stuck": 0.0,
    }
    monkeypatch.setattr(loop, "choose", lambda *args, **kwargs: dict(decision_value))
    runner.command("predict")
    assert runner.state["decision"]["choice"] == "e3"
    assert "skipped" in runner.state["decision"]["recovery"]


def test_irreversible_action_gates_above_the_reversible_floor(runner):
    runner.state["decision"] = {**decision("e3"), "confidence": 0.7, "irreversible": 0.95}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["gate"]["reason"].startswith("irreversible")
    assert state["gate"]["floor"] == 0.9
    runner.state["browser"].act.assert_not_called()


def test_page_level_irreversibility_does_not_raise_a_fill_floor(runner, monkeypatch):
    """A visible Submit button must not put a text field behind the 0.9 floor."""
    from AgentJevUltraFast import gates

    assert gates.check({"confidence": 0.84, "irreversible": 0.9},
                       {"kind": "fill", "label": "Full name"}) is None
    assert gates.check({"confidence": 0.84, "irreversible": 0.9},
                       {"kind": "select", "label": "Ticket type"}) is None
    assert gates.check({"confidence": 0.84, "irreversible": 0.9},
                       {"kind": "click", "label": "Submit"}) is not None
    monkeypatch.setattr(loop, "field_text",
                        lambda *args, **kwargs: ("Ada", {"model": "m", "latency_ms": 1}))
    runner.state["decision"] = {**decision("e1"), "confidence": 0.84, "irreversible": 0.83}
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state.get("gate") is None
    runner.state["browser"].act.assert_called_once()


def test_a_mode_may_lower_the_irreversible_floor():
    from AgentJevUltraFast import gates

    click = {"kind": "click", "label": "Submit", "role": "button"}
    decision = {"confidence": 0.75, "irreversible": 0.9}
    assert gates.check(decision, click) is not None                       # global 0.9
    assert gates.check(decision, click, irreversible_floor=0.7) is None   # user choice
    verdict = gates.check({"confidence": 0.6, "irreversible": 0.9}, click, irreversible_floor=0.7)
    assert verdict is not None and verdict["floor"] == 0.7
    clamped = gates.check({"confidence": 0.4, "irreversible": 0.9}, click, irreversible_floor=0.1)
    assert clamped["floor"] == 0.5                    # never below MODE_FLOOR_MIN


def test_reversible_roles_do_not_inherit_a_submit_buttons_risk():
    from AgentJevUltraFast import gates

    assert gates.check({"confidence": 0.7, "irreversible": 0.9},
                       {"kind": "click", "label": "I confirm", "role": "checkbox"}) is None
    assert gates.check({"confidence": 0.7, "irreversible": 0.9},
                       {"kind": "click", "label": "Read more", "role": "link"}) is None
    verdict = gates.check({"confidence": 0.7, "irreversible": 0.9},
                          {"kind": "click", "label": "Submit", "role": "button"})
    assert verdict is not None and verdict["floor"] == 0.9


def test_reobserve_is_a_read_not_a_mutation(runner):
    runner.state["decision"] = {
        **decision(), "choice": "REOBSERVE", "operation": "REOBSERVE", "target": None,
    }
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "ready"
    assert state["reobserves"] == 1
    runner.state["browser"].act.assert_not_called()


def test_reobserve_is_offered_once_per_page_state(runner, monkeypatch):
    seen = {}

    def fake_choose(*args, **kwargs):
        seen.update(kwargs)
        return {**decision(), "goal_done": 0.0, "stuck": 0.0}

    monkeypatch.setattr(loop, "choose", fake_choose)
    runner.state.pop("reobserved", None)
    runner.command("predict")
    assert seen["allow_reobserve"] is True
    runner.state["reobserved"] = {runner.state["page"]["fingerprint"]: True}
    runner.command("predict")
    assert seen["allow_reobserve"] is False


def test_abstain_ends_the_run_honestly(runner):
    runner.state["decision"] = {
        **decision(), "choice": "ABSTAIN", "operation": "ABSTAIN", "target": None,
    }
    state = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert state["status"] == "abstained"
    assert state["outcome"] == "abstained"
    runner.state["browser"].act.assert_not_called()


def _complete_answer(body, operation, target=None):
    answers = {
        "operation": choice(body["questions"]["operation"]["criteria"], operation),
    }
    for name in body["questions"]:
        if name.endswith("_target"):
            ids = list(body["questions"][name]["criteria"])
            answers[name] = choice(ids, ids[0])
    if target and operation.lower() + "_target" in answers:
        answers[operation.lower() + "_target"] = choice([target], target)
    return {"model": "test-model", "answers": answers, "usage": {"input_tokens": 7}}


def test_escape_hatches_are_offered_and_bounded(monkeypatch):
    seen = []

    def post(_url, _key, body, **_kwargs):
        seen.append(set(body["questions"]["operation"]["criteria"]))
        return _complete_answer(body, "TYPE_TEXT", target="1")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(page(), "Find a book", [])
    model.choose(page(), "Find a book", [], allow_reobserve=False)
    assert {"REOBSERVE", "ABSTAIN"} <= seen[0]
    assert "REOBSERVE" not in seen[1] and "ABSTAIN" in seen[1]


def test_cache_hit_skips_the_model_call(monkeypatch, tmp_path):
    calls = []

    def post(_url, _key, body, **_kwargs):
        calls.append(body)
        return _complete_answer(body, "TYPE_TEXT", target="1")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("JEV_CACHE", "1")
    monkeypatch.setenv("JEV_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(model, "post_json", post)

    first = model.choose(page(), "Find a book", [])
    second = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert first["cached"] is False and first["model"] == "test-model"
    assert second["cached"] is True and second["choice"] == first["choice"]


def test_cache_bypass_is_a_real_second_reading(monkeypatch, tmp_path):
    calls = []

    def post(_url, _key, body, **_kwargs):
        calls.append(body)
        return _complete_answer(body, "TYPE_TEXT", target="1")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("JEV_CACHE", "1")
    monkeypatch.setenv("JEV_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(model, "post_json", post)

    model.choose(page(), "Find a book", [])
    second = model.choose(page(), "Find a book", [], use_cache=False)
    assert len(calls) == 2 and second["cached"] is False


def test_changed_state_is_a_cache_miss(monkeypatch, tmp_path):
    calls = []

    def post(_url, _key, body, **_kwargs):
        calls.append(body)
        return _complete_answer(body, "TYPE_TEXT", target="1")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("JEV_CACHE", "1")
    monkeypatch.setenv("JEV_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(model, "post_json", post)

    model.choose(page(), "Find a book", [])
    changed = {**page(), "url": "https://example.test/results"}
    model.choose(changed, "Find a book", [])
    assert len(calls) == 2


def test_failure_classification_is_free_without_a_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert model.classify_failure(page(), "Find a book", []) is None


def test_failure_classification_parses_a_bounded_choice(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", lambda *_args, **_kwargs: {
        "model": "test",
        "answers": {"failure_class": choice(list(model.FAILURE_CRITERIA), "hard_wall")},
    })
    result = model.classify_failure(page(), "Find a book", [])
    assert result["class"] == "hard_wall" and result["confidence"] == 1.0


def test_evidence_records_decisions_and_outcomes(tmp_path):
    from AgentJevUltraFast.evidence import Evidence

    evidence = Evidence(tmp_path, "run-1", goal="Find a book")
    evidence.record("decision", choice="e1", confidence=0.9)
    evidence.close()
    lines = [
        json.loads(line)
        for line in (tmp_path / "run-1.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["kind"] for line in lines] == ["run_start", "decision"]
    assert lines[0]["goal"] == "Find a book"
    assert lines[1]["choice"] == "e1" and lines[1]["at_ms"] >= 0


def test_evidence_is_optional_and_never_breaks_a_run(runner):
    runner.state["status"] = "done"
    runner._finalize()                       # no evidence writer, no expectation
    assert runner.state["outcome"] == "unknown"


def test_evidence_records_through_the_act_path(runner, tmp_path):
    """Regression: the evidence event name and an action's `kind` field must not collide."""
    from AgentJevUltraFast.evidence import Evidence

    runner.evidence = Evidence(tmp_path, "run-1", goal="Find a book")
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    runner.state["browser"].observe.return_value = {
        **runner.state["page"], "url": "https://example.test/next",
    }
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.evidence.close()
    lines = [
        json.loads(line)
        for line in (tmp_path / "run-1.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    execution = next(line for line in lines if line["kind"] == "execution")
    assert execution["action_kind"] == "click"


# --- v2: descriptor replay -------------------------------------------------------------


def _replay_state(steps):
    return {"available": True, "steps": len(steps), "used": 0, "completed": False,
            "stopped": None, "recorded": None}


def test_replay_plan_roundtrip_is_bound_to_url_and_goal(tmp_path):
    from AgentJevUltraFast import replay

    steps = [{"kind": "click", "label": "Go", "role": "button", "operation": "CLICK"}]
    path = replay.record(tmp_path, "https://example.test/", "Find a book", steps, verified=True)
    assert path.exists()
    assert replay.load(tmp_path, "https://example.test/", "Find a book")["steps"] == steps
    assert replay.load(tmp_path, "https://example.test/", "Find a movie") is None
    assert replay.load(tmp_path, "https://other.test/", "Find a book") is None


def test_replay_matching_is_unique_or_a_miss():
    from AgentJevUltraFast import replay

    actions = [
        {"id": "a", "kind": "click", "label": "Go", "role": "button"},
        {"id": "b", "kind": "click", "label": "  GO ", "role": "link"},
        {"id": "c", "kind": "fill", "label": "Go"},
    ]
    assert replay.match_step({"kind": "click", "label": "Go"}, actions) is None
    assert replay.match_step({"kind": "click", "label": "Go", "role": "link"}, actions)["id"] == "b"
    assert replay.match_step({"kind": "click", "label": "Nope"}, actions) is None
    assert replay.match_step({"kind": "fill", "label": "Go"}, actions)["id"] == "c"


def test_replay_executes_a_recorded_step_without_the_model(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(side_effect=AssertionError("no model call during replay")))
    runner.replay_plan = {
        "steps": [{"kind": "click", "label": "Go", "role": "button", "operation": "CLICK"}]
    }
    runner.state["replay"] = _replay_state(runner.replay_plan["steps"])
    runner.state["decision"] = None
    runner.state["status"] = "ready"
    runner.state["browser"].observe.return_value = {
        **runner.state["page"], "url": "https://example.test/results",
    }
    state = runner.command("tick")
    assert state["status"] == "ready"
    assert state["replay"]["used"] == 1
    assert state["history"][-1]["choice"] == "e3"
    runner.state["browser"].act.assert_called_once()


def test_replay_fill_uses_recorded_literal_without_text_model(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(side_effect=AssertionError("text model called")))
    runner.replay_plan = {"steps": [{
        "kind": "fill", "label": "Search", "role": "textbox",
        "operation": "TYPE_TEXT", "text": "Ada Lovelace",
    }]}
    runner.state["replay"] = _replay_state(runner.replay_plan["steps"])
    runner.state["decision"] = None
    runner._replay_step()
    assert runner.state["browser"].act.call_args.kwargs["text"] == "Ada Lovelace"
    assert runner.state["text_calls"][-1]["model"] == "replay"


def test_replay_never_repeats_an_irreversible_step(runner):
    runner.replay_plan = {
        "steps": [{"kind": "click", "label": "Pay", "operation": "CLICK", "irreversible": 0.95}]
    }
    runner.state["replay"] = _replay_state(runner.replay_plan["steps"])
    runner.state["decision"] = None
    assert runner._replay_step() is False
    assert runner.state["replay"]["stopped"].startswith("irreversible")
    runner.state["browser"].act.assert_not_called()


def test_replay_stops_when_a_step_no_longer_matches(runner):
    runner.replay_plan = {
        "steps": [{"kind": "click", "label": "Gone", "operation": "CLICK"}]
    }
    runner.state["replay"] = _replay_state(runner.replay_plan["steps"])
    runner.state["decision"] = None
    assert runner._replay_step() is False
    assert "does not match" in runner.state["replay"]["stopped"]
    runner.state["browser"].act.assert_not_called()


def test_replay_stops_when_a_step_has_no_effect(runner):
    runner.replay_plan = {
        "steps": [{"kind": "click", "label": "Go", "operation": "CLICK"}]
    }
    runner.state["replay"] = _replay_state(runner.replay_plan["steps"])
    runner.state["decision"] = None
    assert runner._replay_step() is True
    assert "no visible effect" in runner.state["replay"]["stopped"]
    assert runner.state["replay"]["used"] == 0


def test_executed_steps_become_descriptors(runner):
    runner.state["descriptors"] = []
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    runner.state["browser"].observe.return_value = {
        **runner.state["page"], "url": "https://example.test/next",
    }
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    descriptor = runner.state["descriptors"][-1]
    assert descriptor["kind"] == "click"
    assert descriptor["label"] == "Go"
    assert descriptor["outcome"] == "navigated"


def test_completed_run_records_a_replay_plan(runner, tmp_path):
    runner.replay_dir = tmp_path
    runner.expect = {"url_contains": "example.test"}
    runner.state["browser"].evaluate = Mock(return_value={
        "url": "https://example.test/", "title": "Example", "text": "Done",
        "selector_counts": {}, "values": {},
    })
    runner.state["replay"] = _replay_state([])
    runner.state["status"] = "done"
    runner.state["descriptors"] = [{"kind": "click", "label": "Go", "operation": "CLICK"}]
    runner._finalize()
    recorded = runner.state["replay"]["recorded"]
    assert recorded and Path(recorded).exists()


def test_unknown_run_never_records_replay(runner, tmp_path):
    runner.replay_dir = tmp_path
    runner.state["replay"] = _replay_state([])
    runner.state["status"] = "done"
    runner.state["descriptors"] = [{"kind": "click", "label": "Go", "operation": "CLICK"}]
    runner._finalize()
    assert runner.state["replay"]["recorded"] is None


def test_a_replay_run_does_not_rerecord_a_plan(runner, tmp_path):
    runner.replay_dir = tmp_path
    runner.state["replay"] = {**_replay_state([]), "used": 3}
    runner.state["status"] = "done"
    runner.state["descriptors"] = [{"kind": "click", "label": "Go", "operation": "CLICK"}]
    runner._finalize()
    assert runner.state["replay"]["recorded"] is None
    assert not list(tmp_path.glob("*.json"))


def test_provider_failure_retries_once_then_blocks_with_reason(runner, monkeypatch):
    """A decision-provider timeout/outage must end the run honestly, not escape run()."""
    calls = []

    def down(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise RuntimeError("Model connection failed; no action executed.")

    monkeypatch.setattr(loop, "choose", down)
    state = runner.command("predict")
    assert len(calls) == 2
    assert state["status"] == "blocked"
    assert state["outcome"] == "unknown"
    assert "decision provider unavailable" in (runner.state.get("blocked_reason") or "")
    runner.state["browser"].act.assert_not_called()


def test_provider_failure_recovers_on_retry(runner, monkeypatch):
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Model call exceeded its 3s budget; no action executed.")
        return decision("e1")

    monkeypatch.setattr(loop, "choose", flaky)
    state = runner.command("predict")
    assert state["status"] == "predicted" and state["decision"]["choice"] == "e1"


def test_provider_retry_is_not_started_past_the_hard_backstop(runner, monkeypatch):
    def slow_failure(*args, **kwargs):
        # The first attempt used up the wall-clock backstop.
        runner.state["progress_wall_at"] -= loop.STALL_HARD_S
        raise RuntimeError("Model call exceeded its 3s budget; no action executed.")

    monkeypatch.setattr(loop, "choose", slow_failure)
    state = runner.command("predict")
    assert state["status"] == "stalled"
    assert "hard backstop" in state["stall"]["reason"]


@pytest.fixture
def clock(runner, monkeypatch):
    """A controllable monotonic clock, with the runner's stall clocks started on it."""
    now = [1000.0]
    monkeypatch.setattr(loop.time, "monotonic", lambda: now[0])
    runner._mark_progress()
    return now


def test_model_wait_is_excluded_from_the_browser_clock(runner, clock, monkeypatch):
    # A provider latency spike is not the page failing to respond: a 6 s decision (more
    # than the 5 s browser-side window) must not by itself end the run as stalled.
    def slow(*args, **kwargs):
        clock[0] += loop.STALL_LIMIT_S + 1
        return decision("e1")

    monkeypatch.setattr(loop, "choose", slow)
    state = runner.command("predict")
    assert state["status"] == "predicted"
    assert runner.state["model_wait_s"] == pytest.approx(loop.STALL_LIMIT_S + 1)
    assert runner.state["progress_at"] == clock[0]        # fully credited
    assert runner.state["progress_wall_at"] == 1000.0     # the wall clock is not


def test_browser_time_still_counts_after_a_credited_wait(runner, clock, monkeypatch):
    def slow(*args, **kwargs):
        clock[0] += 3.0
        return decision("e1")

    monkeypatch.setattr(loop, "choose", slow)
    clock[0] += 3.0                     # browser-side time before the decision
    runner.command("predict")
    assert not runner.stalled()         # 3 s browser + 3 s model: only 3 s counts
    clock[0] += loop.STALL_LIMIT_S - 2.0
    assert runner.stalled()             # the browser-side 5 s is spent


def test_hard_backstop_bounds_repeated_slow_model_waits(runner, clock, monkeypatch):
    def slow(*args, **kwargs):
        clock[0] += loop.STALL_HARD_S + 0.5
        return decision("e1")

    monkeypatch.setattr(loop, "choose", slow)
    state = runner.command("predict")
    assert state["status"] == "stalled"
    assert "hard backstop" in state["stall"]["reason"]
    assert state["stall"]["hard_limit_s"] == loop.STALL_HARD_S
    runner.state["browser"].act.assert_not_called()


def test_new_page_state_restarts_both_clocks(runner, clock):
    clock[0] += 4.0
    fresh = page()
    fresh["url"] = "https://example.test/next"
    fresh["fingerprint"] = "next"
    assert runner.note_progress(fresh)
    assert runner.state["progress_at"] == runner.state["progress_wall_at"] == clock[0]


def test_read_deadline_is_capped_by_the_hard_backstop(runner, clock):
    runner.state["progress_at"] += 100.0          # as if 100 s of model waits were credited
    assert runner.read_deadline() == 1000.0 + loop.STALL_HARD_S


def test_malformed_expect_fails_at_construction():
    with pytest.raises(ValueError):
        loop.normalize_expect({"text_contain": ["x"]})


def test_background_tab_starts_a_render_keepalive(monkeypatch):
    """Regression: a background tab barely rendered, so JS menus never opened (2026-09-23)."""
    import AgentJevUltraFast.browser as browser

    calls = []
    b = browser.Browser.__new__(browser.Browser)
    b.mode, b.session = "background", "s1"
    monkeypatch.setattr(b, "call", lambda method, **params: calls.append((method, params)) or {})
    b._keep_rendering()
    assert calls and calls[0][0] == "Page.startScreencast" and b.can_screenshot
    calls.clear()
    b.mode = "foreground"
    b.rendering = True
    b._keep_rendering()
    assert calls == []                     # a visible tab renders on its own
    b.mode = "background"
    monkeypatch.setenv("JEV_RENDER_KEEPALIVE", "0")
    b.rendering = False
    b._keep_rendering()
    assert calls == [] and b.can_screenshot is False


def test_screenshot_is_refused_not_hung_on_a_non_rendering_tab(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.rendering = False
    monkeypatch.setattr(browser, "cdp", lambda *a, **k: pytest.fail("no capture attempt"))
    with pytest.raises(RuntimeError, match="not rendering"):
        b.screenshot()
    b.rendering, b.session = True, "s1"
    seen = {}
    monkeypatch.setattr(browser, "cdp", lambda method, **k: seen.update(k) or {"data": "QQ=="})
    assert b.screenshot(timeout=1.5) == "QQ==" and seen["_response_timeout"] == 1.5


def test_reattach_rearms_the_render_keepalive(monkeypatch):
    import AgentJevUltraFast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.mode, b.target, b.session = "background", "t1", "dead"
    b.owns_target, b.owned_targets, b._known_targets = True, ["t1"], {"t1"}
    monkeypatch.setattr(b, "_page_targets", lambda: [{"targetId": "t1", "url": "https://a.test/"}])
    methods = []
    monkeypatch.setattr(browser, "cdp", lambda method, **k: methods.append(method) or {"sessionId": "s2"})
    assert b.reattach() is True
    assert methods == ["Target.attachToTarget", "Page.startScreencast"]


def test_a_decision_that_changed_nothing_is_evicted_from_the_cache(runner, monkeypatch):
    """H2: a cached no-effect click was replayed at 5 ms per lap across runs (Flights v2)."""
    deleted = []
    monkeypatch.setattr(loop.cache, "delete", deleted.append)
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "cache_key": "k-dead",
                                "cached": True}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["outcome"] == "no visible change"
    assert deleted == ["k-dead"] and runner.state["cache_evictions"] == 1


def test_a_productive_decision_stays_cached(runner, monkeypatch):
    deleted = []
    monkeypatch.setattr(loop.cache, "delete", deleted.append)
    moved = {**page(), "url": "https://example.test/results"}
    moved["fingerprint"] = fingerprint(moved)
    runner.state["browser"].observe.return_value = moved
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "cache_key": "k-good"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["outcome"] == "navigated"
    assert deleted == []


def test_text_helper_disables_reasoning_with_the_field_the_endpoint_honours(monkeypatch):
    """{"reasoning": {"enabled": false}} was ignored upstream: 116-178 hidden tokens per field."""
    from AgentJevUltraFast import model

    seen = {}

    def fake_post(url, key, body, headers=None, timeout=None):
        seen.update(body)
        return {"choices": [{"message": {"content": '{"text": "London"}'}}], "usage": {}}

    monkeypatch.setattr(model, "post_json", fake_post)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    output, _ = model.text_call("system", {"field": "Where to?"})
    assert output == {"text": "London"}
    assert seen["reasoning_effort"] == "none" and "reasoning" not in seen


def test_goal_literals_are_not_masked_but_secrets_are():
    browser = Mock(observe=Mock(return_value=page()), private_values=[])
    agent = loop.Agent(
        "https://example.test/", "Search the catalogue for laptops, then sign in",
        values={"Search the catalogue": "laptops", "Password": "laptops-2",
                "Email": "ada@example.test"},
        browser=browser,
    )
    # "laptops" is already in the goal every model receives; hiding it on the page only
    # hid the evidence of success. A value absent from the goal, or under a sensitive
    # label, stays masked.
    assert "laptops" not in agent.private_values
    assert {"laptops-2", "ada@example.test"} <= agent.private_values


def test_a_sensitive_label_stays_private_even_when_the_goal_names_it():
    browser = Mock(observe=Mock(return_value=page()), private_values=[])
    agent = loop.Agent("https://example.test/", "Log in with password hunter22",
                       values={"Password": "hunter22"}, browser=browser)
    assert "hunter22" in agent.private_values


def test_generated_text_stays_visible_to_jev(runner, monkeypatch):
    # A value the text model derived from the goal is already known to the provider;
    # masking it hid the typed query and every autocomplete option that echoed it.
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Zurich", {"model": "t", "latency_ms": 5})))
    runner.private_values = set()
    runner.state["decision"] = decision("e1")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert "Zurich" not in runner.private_values
    assert not getattr(runner, "_private_pixels", False)


def test_private_sources_are_still_masked_once_typed(runner):
    runner.private_values = {"ada@example.test"}
    runner.values = {"Search": "ada@example.test"}
    runner.state["decision"] = decision("e1")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner._private_pixels is True
    assert "ada@example.test" in runner.state["browser"].private_values


def _prefetching(runner, monkeypatch, values):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline")
    batches = []

    def fake(goal, labels, page, **kwargs):
        batches.append((goal, list(labels)))
        return values, {"model": "batch", "latency_ms": 7, "usage": {}}

    monkeypatch.setattr(loop, "field_values", fake)
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e1"))
    return batches


def test_prefetched_value_is_typed_without_a_second_text_call(runner, monkeypatch):
    batches = _prefetching(runner, monkeypatch, {"Search": "dune"})
    monkeypatch.setattr(loop, "field_text", lambda *a, **k: pytest.fail("no single call"))
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert batches == [("Find a book", ["Search"])]
    assert runner.state["browser"].act.call_args.kwargs["text"] == "dune"
    assert runner.state["text_calls"][-1]["prefetched"] is True


def test_prefetch_is_not_reused_for_a_different_goal(runner, monkeypatch):
    _prefetching(runner, monkeypatch, {"Search": "dune"})
    single = Mock(return_value=("atlas", {"model": "single", "latency_ms": 5}))
    monkeypatch.setattr(loop, "field_text", single)
    runner.command("predict")
    runner.state["goal"] = "Find an atlas"
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert single.call_count == 1
    assert runner.state["browser"].act.call_args.kwargs["text"] == "atlas"


def test_null_draft_falls_back_to_the_single_field_call(runner, monkeypatch):
    _prefetching(runner, monkeypatch, {"Search": None})
    single = Mock(return_value=("dune", {"model": "single", "latency_ms": 5}))
    monkeypatch.setattr(loop, "field_text", single)
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert single.call_count == 1


def test_prefetch_skips_sensitive_and_supplied_fields(runner):
    p = page()
    p["actions"] += [
        {"id": "e7", "kind": "fill", "label": "Password", "role": "textbox", "value": "", "node": 70},
        {"id": "e8", "kind": "fill", "label": "Email", "role": "textbox", "value": "", "node": 80},
    ]
    runner.values = {"Email": "ada@example.test"}
    assert runner._prefetch_fields(p) == ["Search"]


def test_no_prefetch_without_a_text_key(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_values", lambda *a, **k: pytest.fail("no paid call"))
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e1"))
    runner.command("predict")
    assert "prefetches" not in runner.state


@pytest.mark.parametrize("reply", [
    {"values": {"Search": "x", "Other": "y"}},      # a label that was not asked for
    {"values": {"Search": 3}},
    {"values": ["x"]},
    {"text": "x"},                                  # a bare mapping must still use asked labels
    {},
])
def test_batch_replies_are_validated_whole(monkeypatch, reply):
    monkeypatch.setattr(model, "text_call", lambda *a, **k: (reply, {"model": "m"}))
    with pytest.raises(ValueError):
        model.field_values("goal", ["Search"], {"title": "t", "text": ""})


def test_batch_reply_keeps_nulls(monkeypatch):
    monkeypatch.setattr(model, "text_call",
                        lambda *a, **k: ({"values": {"Search": "x", "Email": None}}, {"model": "m"}))
    values, _ = model.field_values("goal", ["Search", "Email"], {"title": "t", "text": ""})
    assert values == {"Search": "x", "Email": None}


def test_batch_keys_match_labels_despite_stray_whitespace(monkeypatch):
    monkeypatch.setattr(model, "text_call",
                        lambda *a, **k: ({"values": {"Where to?": "London"}}, {"model": "m"}))
    values, _ = model.field_values("goal", ["Where to? "], {"title": "t", "text": ""})
    assert values == {"Where to? ": "London"}


def test_bare_mapping_batch_is_accepted(monkeypatch):
    monkeypatch.setattr(model, "text_call",
                        lambda *a, **k: ({"Where from?": "Zurich", "Return": None}, {"model": "m"}))
    values, _ = model.field_values("goal", ["Where from?", "Return"], {"title": "t", "text": ""})
    assert values == {"Where from?": "Zurich", "Return": None}


def test_a_draft_covers_pages_whose_fields_it_already_saw(runner, monkeypatch):
    batches = _prefetching(runner, monkeypatch, {"Search": "dune", "Return": None})
    wide = page()
    wide["actions"] = wide["actions"] + [
        {"id": "e9", "kind": "fill", "label": "Return", "role": "textbox", "value": "", "node": 90}]
    runner._maybe_prefetch(wide)
    runner._prefetches[-1]["future"].result(timeout=5)
    runner._maybe_prefetch(page())          # "Return" disappeared: still covered
    assert len(batches) == 1
    extra = page()
    extra["actions"] = extra["actions"] + [
        {"id": "e8", "kind": "fill", "label": "Middle name", "role": "textbox", "value": "", "node": 80}]
    runner._maybe_prefetch(extra)           # a field the draft never saw: draft again
    runner._prefetches[-1]["future"].result(timeout=5)
    assert len(batches) == 2


def test_a_finished_older_draft_is_used_without_waiting(runner, monkeypatch):
    import threading

    _prefetching(runner, monkeypatch, {"Search": "dune"})
    runner._maybe_prefetch(page())
    runner._prefetches[-1]["future"].result(timeout=5)
    gate = threading.Event()
    monkeypatch.setattr(loop, "field_values", lambda *a, **k: gate.wait(5) and ({}, {}))
    grown = page()
    grown["actions"] = grown["actions"] + [
        {"id": "e8", "kind": "fill", "label": "Middle name", "role": "textbox", "value": "", "node": 80}]
    runner._maybe_prefetch(grown)                 # a second draft is now pending
    started = time.monotonic()
    text, meta = runner._take_prefetch(page()["actions"][0], grown)
    gate.set()
    assert text == "dune" and meta["waited_ms"] == 0
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("steps", [["ok", ""], "one string", [3], ["x"] * 31])
def test_plan_must_be_a_short_list_of_steps(steps):
    browser = Mock(observe=Mock(return_value=page()), private_values=[])
    with pytest.raises(ValueError):
        loop.Agent("https://example.test/", "goal", browser=browser, steps=steps)


def test_plan_reaches_the_supervisor(runner, consult):
    calls, script = consult
    script["verdict"] = {"verdict": "hint", "subgoal": "Open billing"}
    runner.steps = ["Open settings", "Open billing"]
    runner._supervise("dead_streak")
    assert calls[0]["plan"] == ["Open settings", "Open billing"]


@pytest.mark.parametrize("label,role,gated", [
    ("Search", "button", False),                # plain clicks execute Jev's argmax
    ("London", "option", False),
    ("Delete project", "button", True),         # a destructive label keeps the 0.5 floor
    ("Deactivate account", "link", True),       # role does not exempt a commit label
    ("Unsubscribe", "link", True),
    ("Change password", "button", True),
    ("I confirm the information is accurate", "checkbox", False),   # a toggle commits nothing
])
def test_commit_labels_keep_a_floor_when_the_noul_reads_low(label, role, gated):
    from AgentJevUltraFast import gates

    verdict = gates.check({"confidence": 0.4, "irreversible": 0.1},
                          {"kind": "click", "label": label, "role": role})
    assert (verdict is not None) is gated


def test_supervisor_act_is_downgraded_when_jevs_reading_is_from_another_page(runner, consult):
    calls, script = consult
    runner.state["decisions"] = [{"irreversible": 0.1, "fingerprint": "some-other-page"}]
    script["verdict"] = {"verdict": "act", "element_id": "e2", "reason": "open it"}
    runner._supervise("dead_streak")
    assert calls[0]["irreversible"] is None
    assert "supervisor_decision" not in runner.state and runner.state["guidance"]


def test_supervisor_act_uses_a_fresh_reading_from_this_page(runner, consult):
    calls, script = consult
    fp = runner.state["page"]["fingerprint"]
    runner.state["decisions"] = [{"irreversible": 0.1, "fingerprint": fp}]
    script["verdict"] = {"verdict": "act", "element_id": "e2", "reason": "open it"}
    runner._supervise("dead_streak")
    assert runner.state["supervisor_decision"]["choice"] == "e2"


def test_delegated_tasks_turn_supervisor_actions_into_hints(runner, consult):
    calls, script = consult
    fp = runner.state["page"]["fingerprint"]
    runner.state["decisions"] = [{"irreversible": 0.1, "fingerprint": fp}]
    runner._delegated = True
    script["verdict"] = {"verdict": "act", "element_id": "e2", "reason": "open it"}
    runner._supervise("dead_streak")
    assert "supervisor_decision" not in runner.state
    assert "Open Search" in runner.state["guidance"]["subgoal"]
    assert runner.state["supervision"][-1]["verdict"] == "hint"


def test_supervision_records_are_redacted(runner, consult):
    calls, script = consult
    runner.private_values = {"hunter22secret"}
    script["verdict"] = {"verdict": "impossible", "reason": "typed hunter22secret but no luck"}
    runner._supervise("dead_streak")
    assert "hunter22secret" not in json.dumps(runner.state["supervision"])


def test_back_is_not_offered_when_the_run_has_not_moved_forward(monkeypatch):
    seen = []

    def post(_url, _key, body, **_kwargs):
        seen.append(set(body["questions"]["operation"]["criteria"]))
        return _complete_answer(body, "TYPE_TEXT", target="1")

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(page(), "Find a book", [])
    model.choose(page(), "Find a book", [], allow_back=False)
    assert "BACK" in seen[0]
    assert "BACK" not in seen[1]


def test_back_is_available_only_above_the_runs_own_history_floor():
    from types import SimpleNamespace

    index = [3]
    agent = loop.Agent.__new__(loop.Agent)
    agent.state = {}
    agent.browser = SimpleNamespace(target="t1", history_index=lambda: index[0])
    assert agent._back_available() is False        # the start page: BACK would leave the task
    index[0] = 4
    assert agent._back_available() is True         # this run navigated forward
    index[0] = 3
    assert agent._back_available() is False        # back at the start again
    agent.browser = SimpleNamespace(target="t2", history_index=lambda: 0)
    assert agent._back_available() is False        # an adopted tab gets its own floor


def test_back_stays_offered_when_history_cannot_be_read():
    from types import SimpleNamespace

    def broken():
        raise RuntimeError("cdp gone")

    agent = loop.Agent.__new__(loop.Agent)
    agent.state = {}
    agent.browser = SimpleNamespace(target="t", history_index=broken)
    assert agent._back_available() is True
    agent.browser = SimpleNamespace(target="t")
    assert agent._back_available() is True
