"""Offline contracts for the vision recovery planner. No paid APIs."""

from AgentJevUltraFast import recovery

PAGE = {"url": "https://example.test/", "title": "T", "text": "", "w": 1000, "h": 500}


def plan(monkeypatch, value):
    monkeypatch.setattr(recovery, "_plan", lambda *args, **kwargs: (value, 7))


def test_click_xy_is_normalized(monkeypatch):
    plan(monkeypatch, {"action": "click_xy", "x": 0.5, "y": 0.25, "reason": "menu"})
    proposal = recovery.propose_action(PAGE, "goal", [], screenshot="img")
    assert proposal["action"]["kind"] == "click_at"
    assert proposal["action"]["x"] == 500 and proposal["action"]["y"] == 125
    assert proposal["reason"] == "menu"


def test_unknown_key_and_action_are_rejected(monkeypatch):
    plan(monkeypatch, {"action": "press_key", "key": "delete"})
    assert recovery.propose_action(PAGE, "g", [], screenshot="img") is None
    plan(monkeypatch, {"action": "type", "text": "hello"})
    assert recovery.propose_action(PAGE, "g", [], screenshot="img") is None


def test_press_key_maps_to_the_closed_vocabulary(monkeypatch):
    plan(monkeypatch, {"action": "press_key", "key": "F", "reason": "fullscreen"})
    proposal = recovery.propose_action(PAGE, "g", [], screenshot="img")
    assert proposal["action"]["kind"] == "key"
    assert proposal["action"]["key"] == "f" and proposal["action"]["vk"] == 70


def test_click_out_of_range_is_rejected(monkeypatch):
    plan(monkeypatch, {"action": "click_xy", "x": 1.5, "y": 0.5})
    assert recovery.propose_action(PAGE, "g", [], screenshot="img") is None


def test_no_screenshot_means_no_escalation():
    assert recovery.propose_action(PAGE, "g", [], screenshot=None) is None


def test_planner_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(recovery, "_plan", boom)
    assert recovery.propose_action(PAGE, "g", [], screenshot="img") is None
