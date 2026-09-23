"""The supervisor may only advise or execute what code can verify on the observed page."""

import pytest

from AgentJevUltraFast import supervisor

PAGE = {
    "url": "https://app.test/form",
    "title": "Apply",
    "text": "Apply for the internship",
    "actions": [
        {"id": "e1", "kind": "fill", "role": "textbox", "label": "Full name", "value": ""},
        {"id": "e2", "kind": "click", "role": "button", "label": "Change ticket type"},
        {"id": "e3", "kind": "click", "role": "button", "label": "Submit application",
         "form_submit": True},
        {"id": "e4", "kind": "click", "role": "checkbox", "label": "I agree"},
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        {"id": "key_enter", "kind": "key", "key": "Enter", "label": "Press Enter"},
        {"id": "e5", "kind": "click", "role": "link", "label": "Unsubscribe"},
    ],
}


def test_hint_is_normalised():
    verdict = supervisor.validate(
        {"verdict": "hint", "subgoal": "  Open the   menu ", "reason": "r"}, PAGE)
    assert verdict == {"verdict": "hint", "subgoal": "Open the menu", "reason": "r"}


@pytest.mark.parametrize("raw", [
    None, "hint", {}, {"verdict": "click_xy", "x": 0.5, "y": 0.5},
    {"verdict": "hint"}, {"verdict": "hint", "subgoal": "   "},
    {"verdict": "act", "element_id": "e99"},                 # not observed
    {"verdict": "act", "element_id": "wait"},                # not an executable kind
    {"verdict": "act", "element_id": "e1"},                  # fill without text
    {"verdict": "act", "element_id": "e1", "text": "x" * 2001},
    {"verdict": "act", "element_id": "e2", "text": "typed"},  # text on a click
    {"verdict": "act", "selector": "#submit"},               # selectors never accepted
])
def test_unusable_replies_are_rejected_not_repaired(raw):
    assert supervisor.validate(raw, PAGE) is None


def test_observed_reversible_click_and_fill_can_execute():
    click = supervisor.validate({"verdict": "act", "element_id": "e2"}, PAGE, irreversible=0.1)
    assert click["verdict"] == "act" and click["action"]["label"] == "Change ticket type"
    assert click["text"] is None
    fill = supervisor.validate({"verdict": "act", "element_id": "e1", "text": "Ada"}, PAGE)
    assert fill["verdict"] == "act" and fill["text"] == "Ada"
    box = supervisor.validate({"verdict": "act", "element_id": "e4"}, PAGE, irreversible=0.99)
    assert box["verdict"] == "act"          # a checkbox is reversible whatever the page risk


def test_committable_clicks_are_downgraded_to_hints():
    submit = supervisor.validate({"verdict": "act", "element_id": "e3"}, PAGE, irreversible=0.0)
    assert submit["verdict"] == "hint" and submit["downgraded_from"] == "e3"
    risky = supervisor.validate({"verdict": "act", "element_id": "e2"}, PAGE, irreversible=0.9)
    assert risky["verdict"] == "hint"


def test_needs_human_vocabulary_is_closed():
    v = supervisor.validate({"verdict": "needs_human", "need": "wire money"}, PAGE)
    assert v == {"verdict": "needs_human", "need": "decision", "reason": ""}
    v = supervisor.validate({"verdict": "needs_human", "need": "login", "reason": "sign in"}, PAGE)
    assert v["need"] == "login"


def test_disabled_or_keyless_supervisor_makes_no_call(monkeypatch):
    monkeypatch.setattr(supervisor, "post_json",
                        lambda *a, **k: pytest.fail("no network call when unconfigured"))
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("SUPERVISOR_API_KEY", raising=False)
    verdict, telemetry = supervisor.consult(PAGE, "goal", [], trigger="stall_deadline")
    assert verdict is None and "disabled" in telemetry["error"]
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setenv("JEV_SUPERVISOR", "off")
    assert supervisor.settings() is None


def test_consult_sends_named_trigger_and_screenshot_and_never_raises(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setenv("JEV_SUPERVISOR", "on")
    seen = {}

    def fake_post(url, key, body, headers=None, timeout=None):
        seen["body"], seen["headers"], seen["timeout"] = body, headers, timeout
        return {"choices": [{"message": {"content": '{"verdict": "act", "element_id": "e2"}'}}],
                "usage": {"prompt_tokens": 10}}

    monkeypatch.setattr(supervisor, "post_json", fake_post)
    verdict, telemetry = supervisor.consult(
        PAGE, "Book a one-way flight", [{"step": 1, "action": "x", "outcome": "no visible change"}],
        trigger="dead_streak", screenshot="QUJD", irreversible=0.1)
    assert verdict["verdict"] == "act" and telemetry["screenshot"] is True
    user = seen["body"]["messages"][1]["content"]
    assert '"trigger": "dead_streak"' in user[0]["text"]
    assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "x-opencode-session" in seen["headers"]

    def boom(*a, **k):
        raise RuntimeError("Model connection failed; no action executed.")

    monkeypatch.setattr(supervisor, "post_json", boom)
    verdict, telemetry = supervisor.consult(PAGE, "goal", [], trigger="stall_deadline")
    assert verdict is None and "RuntimeError" in telemetry["error"]


def test_key_presses_are_never_supervisor_actions():
    # Enter submits forms and key presses are not confidence-gated.
    assert supervisor.validate({"verdict": "act", "element_id": "key_enter"}, PAGE,
                               irreversible=0.0) is None


def test_commit_labelled_link_is_downgraded_whatever_its_role():
    v = supervisor.validate({"verdict": "act", "element_id": "e5"}, PAGE, irreversible=0.0)
    assert v["verdict"] == "hint" and v["downgraded_from"] == "e5"


def test_unknown_reading_counts_as_possibly_irreversible():
    v = supervisor.validate({"verdict": "act", "element_id": "e2"}, PAGE, irreversible=None)
    assert v["verdict"] == "hint"
