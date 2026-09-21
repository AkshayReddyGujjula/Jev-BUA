"""Offline tests for task-class modes: memory, learned values, specialists, collect.

No paid API is reachable from here: conftest clears the keys, JEV_MODES is off by default,
and every model call is monkeypatched at the module boundary.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from jev_ultrafast import agent as agent_module
from jev_ultrafast import modes
from jev_ultrafast.agent import Agent

DECISION = {
    "durable": True,
    "slug": "internship-applications",
    "name": "Internship applications",
    "description": "Filling internship application forms.",
    "keywords": ["internship", "application"],
    "strategy": "decide",
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_MODES_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_MODES", "auto")
    return tmp_path


# --- lifecycle ----------------------------------------------------------------------


def test_slug_sanitizing_refuses_paths():
    assert "/" not in (modes.sanitize_slug("../../etc") or "")
    assert "\\" not in (modes.sanitize_slug("..\\..\\windows") or "")
    assert modes.sanitize_slug("Internship Applications") == "internship-applications"
    assert modes.sanitize_slug("a/b") == "a-b"
    assert modes.sanitize_slug("") is None
    assert len(modes.sanitize_slug("x" * 80)) <= 40


def test_load_refuses_a_traversal_slug(root):
    assert modes.load(root, "../outside") is None
    assert not (root.parent / "outside").exists()


def test_create_writes_memory_and_config_without_the_goal(root):
    mode = modes.create(root, DECISION, "Apply to the internship at Acme with my CV")
    memory = (mode.path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Acme" not in memory                      # goal text never becomes memory
    assert "## About me" in memory and "## Standing instructions" in memory
    data = json.loads((mode.path / "mode.json").read_text(encoding="utf-8"))
    assert data["created_from"].startswith("Apply to the internship")
    assert data["strategy"] == "decide"
    assert data["specialist"]["collect"]["harvest"] is False
    assert (mode.path / "learned").is_dir() and (mode.path / "harvest").is_dir()


def test_create_for_collect_defaults(root):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "scroll")
    assert mode.strategy == "collect"
    assert mode.specialist["collect"]["harvest"] is True
    assert mode.specialist["replay"] is False


def test_creating_an_existing_slug_reuses_the_mode(root):
    first = modes.create(root, DECISION, "one")
    second = modes.create(root, {**DECISION, "name": "Other"}, "two")
    assert second.slug == first.slug
    assert not list(root.glob("internship-applications-2"))


def test_corrupt_mode_json_is_absent_not_fatal(root):
    mode = modes.create(root, DECISION, "x")
    (mode.path / "mode.json").write_text("{not json", encoding="utf-8")
    assert modes.load(root, mode.slug) is None
    assert modes.list_modes(root) == []
    resolved, info = modes.resolve("anything", "", classify=lambda *a: None)
    assert resolved is None and info["matched"] == "none"


# --- matching, routes, resolution ---------------------------------------------------


def test_keyword_match_uses_word_boundaries_and_ignores_urls(root):
    modes.create(root, {**DECISION, "keywords": ["internship"]}, "x")
    mode, info = modes.resolve("Apply to an internship today", "https://jobs.example/x")
    assert mode is not None and info["matched"] == "keyword"
    other, info2 = modes.resolve("Check the weather", "https://internship.example/")
    assert other is None


def test_keyword_tie_goes_to_the_classifier(root):
    modes.create(root, {**DECISION, "slug": "a-mode", "keywords": ["internship"]}, "x")
    modes.create(root, {**DECISION, "slug": "b-mode", "keywords": ["internship"]}, "y")
    calls = []

    def classify(goal, url, existing, timeout):
        calls.append(existing)
        return {"durable": False}

    mode, info = modes.resolve("an internship", "", classify=classify)
    assert mode is None and info["matched"] == "not-durable"
    assert len(calls) == 1


def test_classifier_creates_then_a_route_answers(root):
    calls = []

    def classify(goal, url, existing, timeout):
        calls.append(goal)
        return dict(DECISION)

    goal = "Submit my details to the summer program"
    mode, info = modes.resolve(goal, "", classify=classify)
    assert info["matched"] == "created" and mode.slug == "internship-applications"
    again, info2 = modes.resolve(goal, "", classify=lambda *a: pytest.fail("classifier re-called"))
    assert info2["matched"] == "route" and again.slug == mode.slug
    assert calls == [goal]


def test_a_one_off_is_remembered_as_general(root):
    calls = []

    def classify(goal, url, existing, timeout):
        calls.append(goal)
        return {"durable": False}

    assert modes.resolve("What is the weather", "", classify=classify)[0] is None
    assert modes.resolve("What is the weather", "", classify=classify)[0] is None
    assert len(calls) == 1


def test_general_route_expires(root, monkeypatch):
    key = modes.route_key("weather", "")
    modes.route_put(root, key, "general")
    assert modes.route_get(root, key) == "general"
    monkeypatch.setattr(modes.time, "time", lambda: 10_000_000_000)
    assert modes.route_get(root, key) is None


def test_explicit_mode_loads_and_missing_raises(root):
    modes.create(root, DECISION, "x")
    mode, info = modes.resolve("anything", "", requested="internship-applications")
    assert info["matched"] == "explicit" and mode.slug == "internship-applications"
    with pytest.raises(ValueError):
        modes.resolve("anything", "", requested="nope")


def test_off_returns_none_without_calling_the_classifier(root, monkeypatch):
    monkeypatch.setenv("JEV_MODES", "off")
    mode, info = modes.resolve("x", "", classify=lambda *a: pytest.fail("classifier called"))
    assert mode is None and info["enabled"] is False


# --- memory and learned values ------------------------------------------------------


def test_memory_strips_comments_and_caps(root):
    mode = modes.create(root, DECISION, "x")
    mode.path.joinpath("AGENTS.md").write_text(
        "# Mode\n<!-- secret comment -->\n" + "line\n" * 500, encoding="utf-8"
    )
    text = mode.memory_text()
    assert "secret comment" not in text
    assert len(text) <= modes.MEMORY_MAX + 80
    assert "truncated" in text


def test_learned_values_are_exact_and_sensitive_are_refused(root):
    mode = modes.create(root, DECISION, "x")
    assert mode.learn("Full name", "Ada Lovelace")
    assert mode.learn("Password", "hunter2") is False
    assert mode.value_for("Full name") == "Ada Lovelace"
    assert mode.value_for("  FULL NAME * ") == "Ada Lovelace"
    assert mode.value_for("Name") is None
    assert mode.value_for("Password") is None


def test_learned_memory_never_crosses_modes(root):
    first = modes.create(root, DECISION, "x")
    second = modes.create(root, {**DECISION, "slug": "job-applications", "keywords": []}, "y")
    first.learn("Email", "ada@example.com")
    assert second.value_for("Email") is None


def test_last_learned_value_wins_and_forget_removes(root):
    mode = modes.create(root, DECISION, "x")
    mode.learn("Email", "old@example.com")
    mode.learn("Email", "new@example.com")
    assert mode.value_for("Email") == "new@example.com"
    assert mode.forget("Email") == 2       # both writes are removed, not just the newest
    assert mode.value_for("Email") is None


def test_sensitive_entries_are_filtered_at_read_time(root):
    mode = modes.create(root, DECISION, "x")
    mode.learn("Full name", "Ada")
    path = mode.path / "learned" / f"{os.getpid()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time() + 1, "label": "Card number", "value": "4111"}) + "\n")
    text = mode.memory_text(include_learned=True)
    assert "Ada" in text and "4111" not in text


def test_remember_appends_under_about_me_and_dedupes(root):
    mode = modes.create(root, DECISION, "x")
    assert mode.remember("Name: Ada Lovelace")
    assert mode.remember("Name: Ada Lovelace") is False
    memory = (mode.path / "AGENTS.md").read_text(encoding="utf-8")
    assert memory.index("- Name: Ada Lovelace") > memory.index("## About me")


# --- harvest ------------------------------------------------------------------------


def test_source_match_drops_paraphrases(root):
    text = "Today we shipped a new compiler [item-1]. Celebrity gossip is elsewhere."
    items = [
        {"title": "we shipped a new compiler [item-1]"},
        {"title": "we shipped a brand new compiler"},
        {"title": "x"},
    ]
    kept = modes.source_match(items, text)
    assert [item["title"] for item in kept] == ["we shipped a new compiler [item-1]"]


def test_corrupt_routes_cache_is_treated_as_absent(root):
    (root / "routes.json").write_text("{not json", encoding="utf-8")
    mode, info = modes.resolve("a task", "", classify=lambda *a: {"durable": False})
    assert mode is None and info["matched"] == "not-durable"
    (root / "routes.json").write_text('{"routes": {"x": 3}}', encoding="utf-8")
    assert modes.route_get(root, "x") is None


def test_code_like_labels_are_never_learned(root):
    mode = modes.create(root, DECISION, "x")
    assert mode.learn("Verification code", "123456") is False
    assert mode.learn("Recovery code", "abc-def") is False
    # "code" is blocked conservatively: a postal code is a small loss, a leaked OTP is not.
    assert mode.learn("Postal code", "8001") is False
    assert mode.value_for("Recovery code") is None


def test_harvest_dedupe_and_daily_file(root):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    first = mode.save_items("https://x.test/home", [{"title": "Post one"}, {"title": "Post two"}])
    second = mode.save_items("https://x.test/home", [{"title": "post  ONE"}, {"title": "Post three"}])
    assert len(first) == 2 and len(second) == 1
    files = list(mode.harvest_dir().glob("*.jsonl"))
    assert len(files) == 1
    assert mode.harvest_count() == 3


# --- assertions and model validators ------------------------------------------------


def test_value_and_harvest_assertions():
    from jev_ultrafast.assertions import evaluate_expectations, selectors_for, value_selectors_for

    facts = {
        "url": "http://x/apply", "title": "Apply", "text": "Ready to submit",
        "selector_counts": {}, "values": {"#name": "Ada Lovelace"}, "harvest": {"items": 3},
    }
    ok, detail = evaluate_expectations(
        {"value_contains": {"selector": "#name", "text": "ada"}, "harvest_at_least": 2}, facts
    )
    assert ok and {d["check"] for d in detail} == {"value_contains", "harvest_at_least"}
    assert not evaluate_expectations({"harvest_at_least": 5}, facts)[0]
    assert selectors_for({"selector_count": {"selector": ".item"}}) == [".item"]
    assert value_selectors_for({"value_contains": {"selector": "#name", "text": "x"}}) == ["#name"]


def test_page_facts_reads_values_through_the_browser():
    from jev_ultrafast.assertions import page_facts

    class Browser:
        def evaluate(self, script):
            if "location.href" in script:
                return "http://x/apply"
            if "document.title" in script:
                return "Apply"
            if "innerText" in script:
                return "body"
            if ".value" in script:
                return "Ada"
            return 0

    facts = page_facts(Browser(), [], ["#name"])
    assert facts["values"]["#name"] == "Ada"


def test_classify_mode_validates(monkeypatch):
    from jev_ultrafast import model

    monkeypatch.setattr(model, "text_call", lambda *a, **k: (
        {"durable": True, "slug": "X", "name": "N", "keywords": ["a", 3], "strategy": "weird"}, {}
    ))
    decision = model.classify_mode("goal", "url", [], timeout=1)
    assert decision["durable"] and decision["strategy"] == "decide"
    assert decision["keywords"] == ["a"]
    monkeypatch.setattr(model, "text_call", lambda *a, **k: ({"durable": "false"}, {}))
    assert model.classify_mode("g", "u", []) == {"durable": False}
    monkeypatch.setattr(model, "text_call", lambda *a, **k: (["not a dict"], {}))
    assert model.classify_mode("g", "u", []) is None
    monkeypatch.setattr(model, "text_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert model.classify_mode("g", "u", []) is None


def test_classify_gets_a_longer_budget_than_the_loop_helper(monkeypatch):
    from jev_ultrafast import model

    seen = {}
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline-test")
    monkeypatch.setattr(model, "post_json", lambda url, key, body, **kwargs: seen.update(
        kwargs) or {"choices": [{"message": {"content": '{"durable": false}'}}]})
    assert model.classify_mode("g", "u", []) == {"durable": False}
    assert seen["timeout"] > model.TEXT_TIMEOUT


def test_harvest_items_validates(monkeypatch):
    from jev_ultrafast import model

    monkeypatch.setattr(model, "text_call", lambda *a, **k: ({"items": [
        {"title": "A real headline", "note": "why"},
        {"title": "x"},
        {"note": "no title"},
    ]}, {}))
    assert model.harvest_items("page", "memory") == [{"title": "A real headline", "note": "why"}]
    monkeypatch.setattr(model, "text_call", lambda *a, **k: ({"items": []}, {}))
    assert model.harvest_items("page", "memory") is None


# --- agent wiring -------------------------------------------------------------------


SCROLL = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 600}


def page(url="http://x/feed", text="feed", actions=None, fingerprint=None):
    actions = actions if actions is not None else [
        SCROLL,
        {"id": "e1", "kind": "fill", "label": "Full name", "role": "textbox", "value": "", "node": 1},
    ]
    return {"url": url, "title": "t", "text": text, "actions": actions,
            "fingerprint": fingerprint or url}


class FakeBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.index = 0
        self.acted = []
        self.last_url = self.pages[0]["url"]

    def fresh(self, _page):
        return True

    def act(self, action, _page, text=None):
        self.acted.append({"action": action, "text": text})
        self.index = min(self.index + 1, len(self.pages) - 1)

    def observe(self, screenshot=True, deadline=None):
        return self.pages[self.index]

    def follow_new_tab(self):
        return False

    def scroll_content(self, delta=600):
        self.wheeled = getattr(self, "wheeled", 0) + 1
        return 0

    def evaluate(self, script):
        current = self.pages[self.index]
        if "location.href" in script:
            return current["url"]
        if "document.title" in script:
            return current["title"]
        if "innerText" in script:
            return current["text"]
        return 0


def make_agent(page_state, *, mode=None, browser=None):
    agent = Agent.__new__(Agent)
    agent.mode = mode
    agent._mode_info = {"enabled": bool(mode), "matched": "explicit" if mode else "off"}
    agent._strategy = mode.strategy if mode else "decide"
    agent._operation_model = None
    agent._text_model = None
    agent._text_base_url = None
    agent.decide_timeout = 3.0
    agent._max_steps = None
    agent.use_cache = True
    collect = (mode.specialist.get("collect") if mode else None) or {}
    agent._harvest_enabled = bool(collect.get("harvest")) and agent._strategy == "collect"
    agent._harvest_instructions = None
    agent._collect_limit = int(collect.get("steps") or 4)
    agent._irreversible_floor = (mode.specialist.get("irreversible_floor") if mode else None)
    agent.replay_dir = None
    agent.replay_plan = None
    agent.values = {}
    agent.find = ""
    agent.expect = None
    agent.evidence = None
    agent.record_dir = None
    agent.screenshots = False
    agent._owns_browser = False
    agent.pending_text = None
    agent.start_url = "http://x/"
    agent.browser = browser or FakeBrowser([page_state])
    agent.state = {
        "browser": agent.browser, "goal": "goal", "page": page_state, "decision": None,
        "history": [], "status": "ready", "plan": ["goal"], "plan_index": 0,
        "decisions": [], "text_calls": [], "elapsed_ms": 0, "started_at": time.perf_counter(),
        "record": False, "progress_at": time.monotonic(), "stall_rounds": 0,
        "recent_fingerprints": [], "stall": None, "outcome": None, "checks": None,
        "readback": None, "gate": None, "reobserves": 0, "failure_class": None,
        "cache_hits": 0, "cache_misses": 0, "evidence": None, "descriptors": [],
        "replay": {"available": False, "steps": 0, "used": 0, "completed": False,
                   "stopped": None, "recorded": None},
        "mode": mode.summary() if mode else None, "mode_matched": "explicit",
        "harvest": {"items": 0, "calls": 0, "dropped": 0, "steps": 0, "barren": 0,
                    "file": None, "stopped": None, "last": []},
        "seen_labels": [],
    }
    return agent


def decision(choice="e1", operation="TYPE_TEXT", target="1", confidence=0.9, **extra):
    value = {
        "choice": choice, "operation": operation, "target": target, "confidence": confidence,
        "probability": confidence, "goal_done": 0.0, "stuck": 0.0, "irreversible": 0.0,
        "candidate_map": {}, "raw_answers": {}, "usage": {}, "latency_ms": 1,
        "cached": False, "model": "test",
    }
    value.update(extra)
    return value


def internship_mode(root):
    return modes.create(root, DECISION, "Apply to the internship")


def test_mode_context_reaches_the_decision_request(monkeypatch, root):
    mode = internship_mode(root)
    seen = {}

    def fake_choose(*args, **kwargs):
        seen.update(kwargs)
        return decision()

    monkeypatch.setattr(agent_module, "choose", fake_choose)
    agent = make_agent(page(), mode=mode)
    agent.command("predict")
    assert seen["mode_state"]["name"] == mode.name
    assert "memory" in seen["mode_state"]


def test_text_context_includes_mode_memory(root):
    from jev_ultrafast import model

    mode = internship_mode(root)
    mode.remember("Name: Ada Lovelace")
    context = model.field_context(
        "goal", {"label": "Full name", "role": "textbox", "value": ""},
        {"title": "t", "text": "body"}, [], mode_state=mode.text_state(),
    )
    assert "Ada Lovelace" in context["mode"]["memory"]


def test_learned_value_is_typed_without_a_model_call(monkeypatch, root):
    mode = internship_mode(root)
    mode.learn("Full name", "Ada Lovelace")
    agent = make_agent(page(), mode=mode)
    calls = []
    monkeypatch.setattr(
        agent_module, "field_text",
        lambda *a, **k: calls.append(1) or ("model-wrote-this", {"model": "m", "latency_ms": 1}),
    )
    agent.state["decision"] = decision()
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert calls == []
    assert agent.state["text_calls"][-1]["model"] == "mode-memory"
    assert agent.browser.acted[-1]["text"] == "Ada Lovelace"


def test_agent_uses_the_modes_irreversible_floor(root, monkeypatch):
    mode = internship_mode(root)
    mode.data.setdefault("specialist", {})["irreversible_floor"] = 0.6
    submit = page(actions=[
        SCROLL,
        {"id": "e9", "kind": "click", "label": "Submit application", "role": "button", "node": 2},
    ])
    agent = make_agent(submit, mode=mode)
    agent.state["decision"] = decision("e9", operation="CLICK", confidence=0.65, irreversible=0.92)
    state = agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert state.get("gate") is None
    assert agent.browser.acted and agent.browser.acted[-1]["action"]["id"] == "e9"


def test_default_floor_still_withholds_the_same_submit(root):
    mode = internship_mode(root)                     # no floor override: global 0.9
    submit = page(actions=[
        SCROLL,
        {"id": "e9", "kind": "click", "label": "Submit application", "role": "button", "node": 2},
    ])
    agent = make_agent(submit, mode=mode)
    agent.state["decision"] = decision("e9", operation="CLICK", confidence=0.65, irreversible=0.92)
    state = agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert state["gate"]["floor"] == 0.9
    assert not agent.browser.acted


def test_mcp_tool_calls_are_bounded_by_a_deadline():
    import time as time_module

    import jev_mcp_server

    assert jev_mcp_server._bounded(lambda args: {"ok": args}, {"x": 1}, timeout=1) == {"ok": {"x": 1}}
    started = time_module.monotonic()
    result = jev_mcp_server._bounded(lambda args: time_module.sleep(5), {}, timeout=0.2)
    assert result["abandoned"] is True
    assert time_module.monotonic() - started < 3

    def boom(args):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        jev_mcp_server._bounded(boom, {}, timeout=1)


def test_an_unambiguous_memory_fill_skips_the_confidence_gate(monkeypatch, root):
    """Both element (unique exact label) and value (user memory) are code-validated."""
    mode = internship_mode(root)
    mode.learn("Full name", "Ada Lovelace")
    agent = make_agent(page(), mode=mode)
    agent.state["decision"] = decision(confidence=0.1)
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert agent.browser.acted[-1]["text"] == "Ada Lovelace"
    assert agent.state["text_calls"][-1]["model"] == "mode-memory"


def test_an_ambiguous_memory_fill_still_faces_the_gate(monkeypatch, root):
    """Two fields with the same label are exactly the wrong-field risk: gate them."""
    mode = internship_mode(root)
    mode.learn("Email", "ada@example.test")
    ambiguous = page(actions=[
        SCROLL,
        {"id": "e1", "kind": "fill", "label": "Email", "role": "textbox", "value": "", "node": 1},
        {"id": "e2", "kind": "fill", "label": "Email (required)", "role": "textbox",
         "value": "", "node": 2},
    ])
    agent = make_agent(ambiguous, mode=mode)
    agent.state["decision"] = decision(confidence=0.1)
    state = agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert state["gate"]["resolution"] == "reobserve"
    assert agent.browser.acted == []


def test_memory_fill_executes_above_the_floor(monkeypatch, root):
    mode = internship_mode(root)
    mode.learn("Full name", "Ada Lovelace")
    agent = make_agent(page(), mode=mode)
    agent.state["decision"] = decision(confidence=0.9)
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert agent.browser.acted[-1]["text"] == "Ada Lovelace"
    assert agent.state["text_calls"][-1]["model"] == "mode-memory"


def test_learning_requires_the_field_to_have_accepted_the_value(monkeypatch, root):
    mode = internship_mode(root)
    before = page()
    after = page(fingerprint="after", actions=[
        SCROLL,
        {"id": "e1", "kind": "fill", "label": "Full name", "role": "textbox",
         "value": "Ada Lovelace", "node": 1},
    ])
    browser = FakeBrowser([before, after])
    agent = make_agent(before, mode=mode, browser=browser)
    monkeypatch.setattr(
        agent_module, "field_text",
        lambda *a, **k: ("Ada Lovelace", {"model": "m", "latency_ms": 1}),
    )
    agent.state["decision"] = decision()
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert mode.value_for("Full name") == "Ada Lovelace"


def test_a_rejected_fill_is_not_learned(monkeypatch, root):
    mode = internship_mode(root)
    before = page()
    after = page()                      # the field stayed empty
    browser = FakeBrowser([before, after])
    agent = make_agent(before, mode=mode, browser=browser)
    monkeypatch.setattr(
        agent_module, "field_text",
        lambda *a, **k: ("Ada Lovelace", {"model": "m", "latency_ms": 1}),
    )
    agent.state["decision"] = decision()
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert mode.learned_entries() == []


def test_sensitive_fill_is_never_learned(monkeypatch, root):
    mode = internship_mode(root)
    before = page(actions=[{"id": "e1", "kind": "fill", "label": "Password",
                            "role": "textbox", "value": "", "node": 1}])
    after = page(actions=[{"id": "e1", "kind": "fill", "label": "Password",
                           "role": "textbox", "value": "hunter2", "node": 1}])
    browser = FakeBrowser([before, after])
    agent = make_agent(before, mode=mode, browser=browser)
    monkeypatch.setattr(
        agent_module, "field_text",
        lambda *a, **k: ("hunter2", {"model": "m", "latency_ms": 1}),
    )
    agent.state["decision"] = decision()
    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
    assert mode.learned_entries() == []


def test_replay_descriptor_redacts_sensitive_text(root):
    assert modes.redact_for_replay("Password", "hunter2") is None
    assert modes.redact_for_replay("Full name", "Ada") == "Ada"


def test_collect_only_scrolls_and_harvests_once_per_page(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    first = page(text="feed page one with a post " + "and more words to pass the harvest floor. " * 2,
                 fingerprint="p1")
    second = page(url="http://x/feed2",
                  text="feed page two with another post " + "and more words to pass the floor. " * 2,
                  fingerprint="p2")
    browser = FakeBrowser([first, second])
    agent = make_agent(first, mode=mode, browser=browser)
    agent._collect_limit = 2
    harvest_calls = []
    monkeypatch.setattr(
        agent_module, "harvest_items",
        lambda *a, **k: harvest_calls.append(1) or [{"title": "feed page one", "note": ""}],
    )
    agent.command("tick")
    agent.command("tick")
    assert [a["action"]["kind"] for a in browser.acted] == ["scroll", "scroll"]
    assert len(harvest_calls) == 2                  # one per page state, not per tick
    assert all(h["kind"] == "scroll" for h in agent.state["history"])


def test_collect_stops_when_the_feed_exhausts(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    browser = FakeBrowser([page(text="one post that never grows", fingerprint="same")])
    agent = make_agent(browser.pages[0], mode=mode, browser=browser)
    agent._collect_limit = 20
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    for _ in range(6):
        agent.command("tick")
    assert agent.state["status"] == "done"
    assert agent.state["harvest"]["stopped"] == "feed exhausted"
    # The first tick seeds the content hash, then three unchanged reads count as barren.
    assert agent.state["harvest"]["steps"] == agent_module.BARREN_SCROLL_LIMIT


def test_collect_counts_revealed_text_as_progress(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    filler = "enough text to pass the harvest floor. " * 3
    first = page(text="first post. " + filler, fingerprint="same")
    second = page(text="first post. second post appeared. " + filler, fingerprint="same")
    browser = FakeBrowser([first, second])
    agent = make_agent(first, mode=mode, browser=browser)
    agent._collect_limit = 3
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    agent.command("tick")
    assert agent.state["harvest"]["barren"] == 0      # visible text grew: still collecting
    assert agent.state["collect_text_hash"]           # the collector tracks content, not controls


def test_collect_waits_for_a_feed_that_has_not_rendered(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    shell = page(text="loading shell with no posts yet, but enough words to pass " * 2,
                 actions=[], fingerprint="shell")
    feed = page(url="http://x/feed", text="a rendered post appears here, long enough " * 3,
                actions=[SCROLL], fingerprint="feed")

    class SettlingBrowser(FakeBrowser):
        def __init__(self, pages):
            super().__init__(pages)
            self.observations = 0

        def observe(self, screenshot=True, deadline=None):
            self.observations += 1
            return self.pages[0] if self.observations <= 1 else self.pages[1]

    browser = SettlingBrowser([shell, feed])
    agent = make_agent(shell, mode=mode, browser=browser)
    agent._collect_limit = 1
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    agent.command("tick")          # shell: no scroll yet, wait and re-observe
    agent.command("tick")          # the feed has rendered
    assert agent.state["page"]["fingerprint"] == "feed"
    assert agent.state["collect_settle"] >= 1
    agent.command("tick")          # now it can scroll
    assert agent.state["history"][-1]["kind"] == "scroll"


def test_collect_gives_up_after_bounded_settle_attempts(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    browser = FakeBrowser([page(text="a shell that never renders, with enough words " * 3,
                                actions=[], fingerprint="shell")])
    agent = make_agent(browser.pages[0], mode=mode, browser=browser)
    agent._collect_wheel_limit = 0                 # no wheel fallback: pure settle path
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    for _ in range(agent_module.COLLECT_SETTLE_ATTEMPTS + 2):
        agent.command("tick")
    assert agent.state["status"] == "done"
    assert agent.state["harvest"]["stopped"] == "no scroll offered"


def test_collect_uses_a_content_wheel_when_no_scroll_action_is_offered(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    browser = FakeBrowser([page(text="an inner-scroller feed with enough words " * 3,
                                actions=[], fingerprint="inner")])
    agent = make_agent(browser.pages[0], mode=mode, browser=browser)
    agent._collect_settle_attempts = 0             # skip the render wait for this unit test
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    agent.command("tick")
    assert agent.state["history"][-1]["kind"] == "wheel"
    assert browser.wheeled == 1


def test_collect_verifies_from_harvest_and_page_checks(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    body = "[item-1] AI engineering is fun " + "with enough text to pass the harvest floor. " * 2
    browser = FakeBrowser([page(text=body, fingerprint="f1")])
    agent = make_agent(browser.pages[0], mode=mode, browser=browser)
    agent._collect_limit = 1
    agent.expect = {"harvest_at_least": 1, "text_contains": ["[item-1]"]}
    monkeypatch.setattr(
        agent_module, "harvest_items",
        lambda *a, **k: [{"title": "[item-1] AI engineering is fun", "note": ""}],
    )
    agent.command("tick")
    agent.command("tick")
    assert agent.state["status"] == "done"
    assert agent.state["outcome"] == "verified"
    assert agent.state["harvest"]["items"] == 1


def test_collect_reports_steps_when_the_limit_is_reached(root, monkeypatch):
    mode = modes.create(root, {**DECISION, "strategy": "collect"}, "x")
    first = page(text="post one", fingerprint="p1")
    second = page(url="http://x/2", text="post two", fingerprint="p2")
    browser = FakeBrowser([first, second])
    agent = make_agent(first, mode=mode, browser=browser)
    agent._collect_limit = 1
    monkeypatch.setattr(agent_module, "harvest_items", lambda *a, **k: None)
    agent.command("tick")
    agent.command("tick")
    assert agent.state["harvest"]["stopped"] == "steps"
    assert agent.state["status"] == "done"


# --- CLI and MCP plumbing -----------------------------------------------------------


def test_run_task_remember_writes_mode_memory(root, monkeypatch):
    import run_task

    mode = internship_mode(root)
    monkeypatch.setenv("JEV_MODES", "off")
    monkeypatch.setattr(sys, "argv", [
        "run_task.py", "--goal", "x", "--url", "http://x/", "--tab", "background",
        "--mode", "internship-applications", "--remember", "Name: Ada Lovelace",
    ])
    assert run_task.main() == 0
    assert "Name: Ada Lovelace" in (mode.path / "AGENTS.md").read_text(encoding="utf-8")


def test_run_task_remember_needs_an_explicit_mode(root, monkeypatch):
    import run_task

    internship_mode(root)
    monkeypatch.setattr(sys, "argv", [
        "run_task.py", "--goal", "x", "--url", "http://x/", "--tab", "background",
        "--mode", "auto", "--remember", "Name: Ada",
    ])
    with pytest.raises(SystemExit) as error:
        run_task.main()
    assert error.value.code == 2


def test_mcp_browser_do_exposes_mode_and_collect_steps():
    import jev_mcp_server

    tool = next(t for t in jev_mcp_server.TOOLS if t.get("name") == "browser_do")
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    assert "mode" in properties and "collect_steps" in properties


def test_benchmark_scripts_force_modes_off():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("measure_flights.py", "record_flights.py", "verify_stall.py",
                 "benchmark_flights.py", "run_checks.py"):
        text = (scripts / name).read_text(encoding="utf-8")
        assert 'JEV_MODES"] = "off"' in text, name


def test_resolve_degrades_when_creation_fails(root, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("read-only root")

    monkeypatch.setattr(modes, "create", boom)
    mode, info = modes.resolve("a brand new task", "", classify=lambda *a: dict(DECISION))
    assert mode is None and info["matched"] == "none"
    assert "read-only" in info["error"]


def test_decision_request_carries_memory_and_changes_the_cache_key(monkeypatch):
    from jev_ultrafast import model

    captured = []

    def fake_post(url, key, payload, **kwargs):
        captured.append(payload)
        return {
            "answers": {
                "operation": {"choice": "CLICK", "confidence": 0.9, "probabilities": {
                    "CLICK": 0.95, "DONE": 0.01, "BACK": 0.01, "BLOCKED": 0.01,
                    "REOBSERVE": 0.01, "ABSTAIN": 0.01}},
                "goal_done": {"noul": 0.0},
                "stuck": {"noul": 0.0},
                "irreversible": {"noul": 0.0},
                "click_target": {"choice": "1", "confidence": 0.9,
                                 "probabilities": {"1": 1.0}},
            },
            "model": "m", "usage": {},
        }

    monkeypatch.setattr(model, "post_json", fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test")
    monkeypatch.setattr(model.cache, "load", lambda key: None)
    monkeypatch.setattr(model.cache, "store", lambda key, result: None)
    page_state = {
        "url": "u", "title": "t", "text": "x",
        "actions": [{"id": "e1", "kind": "click", "label": "Go", "node": 1, "role": "button"}],
    }
    first = model.choose(page_state, "goal", [], mode_state={"name": "m", "memory": "Ada"})
    second = model.choose(page_state, "goal", [], mode_state={"name": "m", "memory": "Bob"})
    assert captured[0]["state"]["mode"]["memory"] == "Ada"
    assert first["cache_key"] != second["cache_key"]


def test_retry_context_changes_with_mode_memory():
    from jev_ultrafast import model

    action = {"label": "Full name", "role": "textbox", "value": ""}
    page_state = {"title": "t", "text": "x"}
    first = model.field_context("g", action, page_state, [],
                                mode_state={"name": "m", "memory": "Ada"})
    second = model.field_context("g", action, page_state, [],
                                 mode_state={"name": "m", "memory": "Bob"})
    assert first != second
