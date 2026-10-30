"""Offline router calibration: predictions are never their own labels."""

import json

from AgentJevUltraFast import modes, router
from AgentJevUltraFast.cost import summarize


def six_modes(root):
    result = []
    for index in range(6):
        result.append(modes.create(root, {
            "slug": f"class-{index}", "name": f"Class {index}",
            "description": f"Repeated task kind {index}",
            "keywords": [f"special-{index}"], "strategy": "decide",
        }, f"seed-{index}"))
    return result


def proposal(choice):
    return {"choice": choice, "confidence": 0.99, "margin": 0.7,
            "probabilities": {choice: 0.8}, "model": "mock", "usage": {}, "latency_ms": 1}


def test_enforcement_needs_independent_labels_for_current_candidates(tmp_path):
    known = six_modes(tmp_path)
    assert router.grade(tmp_path, known)["ready"] is False
    choices = [item.slug for item in known] + ["new_class", "one_off"]
    for index in range(32):
        goal = f"unseen task {index}"
        chosen = choices[index % len(choices)]
        router.record(tmp_path, goal, "https://example.test/", known, proposal(chosen), "not-a-label")
        with (tmp_path / "routing_labels.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"task_key": router.task_key(goal, "https://example.test/"),
                                     "expected": chosen}) + "\n")
    grade = router.grade(tmp_path, known)
    assert grade["ready"] is True and grade["labelled"] == 32
    changed = [*known]
    changed[0].data["description"] = "changed candidate meaning"
    assert router.grade(tmp_path, changed)["ready"] is False


def test_shadow_never_overrides_text_classification(tmp_path, monkeypatch):
    known = six_modes(tmp_path)
    monkeypatch.setenv("JEV_MODES_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_ROUTER", "shadow")
    monkeypatch.setattr(router, "propose", lambda *a, **k: proposal(known[0].slug))
    chosen, info = modes.resolve(
        "unseen browser errand", "https://example.test/",
        requested="auto", classify=lambda *a: {"durable": False},
    )
    assert chosen is None and info["matched"] == "not-durable"
    log = (tmp_path / "routing.jsonl").read_text(encoding="utf-8")
    assert '"baseline": "one_off"' in log


def test_enforce_can_choose_an_existing_mode_only_after_gate(tmp_path, monkeypatch):
    known = six_modes(tmp_path)
    monkeypatch.setenv("JEV_MODES_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_ROUTER", "enforce")
    monkeypatch.setattr(router, "propose", lambda *a, **k: proposal(known[0].slug))
    monkeypatch.setattr(router, "grade", lambda *a, **k: {"ready": True})
    chosen, info = modes.resolve(
        "unseen browser errand", "https://example.test/", requested="auto",
        classify=lambda *a: (_ for _ in ()).throw(AssertionError("text classifier called")),
    )
    assert chosen.slug == known[0].slug and info["matched"] == "jev-router"


def test_cost_estimate_requires_reported_usage():
    state = {
        "decisions": [{"usage": {"input_tokens": 1000}, "cached": False},
                      {"usage": {}, "cached": True}],
        "router": {"usage": {"prompt_tokens": 500}},
        "text_calls": [{"model": "helper", "usage": {"prompt_tokens": 200}}],
        "cache_hits": 1, "cache_misses": 1,
    }
    report = summarize(state)
    assert report["jev_input_tokens"] == 1500
    assert report["jev_input_usd_estimate"] == 0.000063
    assert report["text_input_tokens"] == 200
    assert report["side_usd_reported"] == 0
    assert report["calls"]["jev"] == 2 and report["calls"]["jev_cached"] == 1
    state["decisions"][0]["usage"] = {}
    assert summarize(state)["jev_input_usd_estimate"] is None


def test_cost_counts_reviewer_calls_from_the_providers_own_figure():
    state = {
        "decisions": [{"usage": {"input_tokens": 1_000_000}, "hedged": True}],
        "text_calls": [{"model": "supplied", "usage": {}}],
        "reviews": [{"usage": {"prompt_tokens": 358, "opencode": {"list_cost": 7.05e-05}},
                     "error": None},
                    {"usage": {}, "error": "RuntimeError: HTTP 401"}],
    }
    report = summarize(state)
    assert report["calls"] == {"jev": 1, "jev_hedges": 1, "jev_cached": 0, "text": 0,
                               "review": 1, "supervisor": 0}
    assert report["side_usd_reported"] == 7.05e-05
    assert report["usd_estimate"] == round(0.042 + 7.05e-05, 8)
