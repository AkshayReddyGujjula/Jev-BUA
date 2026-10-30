"""The parallel reviewer: never on the critical path, only ever a sub-goal."""

import threading

from test_agent import runner  # noqa: F401  (shared fixture)

from AgentJevUltraFast import reviewer

CONFIG = {"limit": 3, "model": "m", "base": "http://x", "key": "k", "timeout": 1, "reasoning": "none"}


def test_submit_never_waits_and_the_newest_snapshot_replaces_a_queued_one():
    gate = threading.Event()
    seen = []

    def slow(payload, _config):
        seen.append(payload["step"])
        gate.wait(2)
        return {"on_track": True, "step": payload["step"]}

    r = reviewer.Reviewer(CONFIG, call=slow)
    r.submit({"step": 1})
    r.submit({"step": 2})                 # queued behind the in-flight review
    r.submit({"step": 3})                 # replaces step 2
    assert r.poll() is None               # in flight: poll returns immediately
    gate.set()
    r.future.result(2)
    assert r.poll()["step"] == 1          # finishing starts the newest pending one
    r.future.result(2)
    assert seen == [1, 3]
    r.close()


def test_the_per_run_limit_bounds_calls():
    r = reviewer.Reviewer({**CONFIG, "limit": 1}, call=lambda p, c: {"on_track": True})
    r.submit({"step": 1})
    r.future.result(1)
    r.poll()
    r.submit({"step": 2})
    assert r.sent == 1
    r.close()


def test_off_track_without_a_subgoal_is_not_usable(monkeypatch):
    monkeypatch.setattr(reviewer, "post_json", lambda *a, **k: {
        "choices": [{"message": {"content": '{"on_track": false, "reason": "hmm"}'}}]})
    result = reviewer.review({"step": 1, "page": {"url": "u"}}, CONFIG)
    assert result["on_track"] is None and result["error"]


def test_an_off_track_review_becomes_guidance_only_on_the_same_page(runner):  # noqa: F811
    class Done:
        def __init__(self, result):
            self.result = result

        def poll(self):
            result, self.result = self.result, None
            return result

    url = runner.state["page"].get("url")
    runner._reviewer = Done({"on_track": False, "subgoal": "Search for MrBeast", "url": "elsewhere"})
    runner._review_poll()
    assert runner.state.get("guidance") is None
    assert runner.state["reviews"][-1]["applied"] is False
    runner._reviewer = Done({"on_track": False, "subgoal": "Search for MrBeast", "url": url})
    runner._review_poll()
    assert runner.state["guidance"]["subgoal"] == "Search for MrBeast"
    assert runner.state["guidance"]["source"] == "reviewer"


def test_done_waits_for_a_verdict_on_this_state_and_is_overridden_at_most_twice(runner):  # noqa: F811
    url = runner.state["page"].get("url")

    class Waiting:
        config = {"timeout": 1}
        submitted = []

        def submit(self, payload):
            self.submitted.append(payload)

        def wait_for(self, step, _timeout):
            return [{"on_track": False, "subgoal": "Set the speed to 2x", "url": url, "step": step}]

    runner._reviewer = Waiting()
    assert runner._review_before_done() is True
    assert Waiting.submitted[-1]["agent_chose_done"] is True
    assert runner.state["guidance"]["subgoal"] == "Set the speed to 2x"
    assert runner._review_before_done() is True
    assert runner._review_before_done() is False       # REVIEW_DONE_LIMIT reached: DONE stands
