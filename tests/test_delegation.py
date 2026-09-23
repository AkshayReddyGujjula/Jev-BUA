"""Offline contract for a delegated browser task and its human checkpoints."""

import time

import pytest

from AgentJevUltraFast.delegation import DelegatedTask, TaskRegistry, approval_boundary


def page(*, password=False, action=None, fingerprint="page-1"):
    return {
        "url": "https://example.test/setup",
        "title": "Setup",
        "fingerprint": fingerprint,
        "human": {"password": password, "one_time_code": False, "captcha": False},
        "actions": [action] if action else [],
    }


class FakeBrowser:
    def __init__(self, next_page):
        self.next_page = next_page
        self.observations = 0
        self.closed = False
        self.private_values = []

    def observe(self, screenshot=False):
        self.observations += 1
        return self.next_page

    def close(self):
        self.closed = True


class FakeAgent:
    def __init__(self, initial_page, choice, irreversible=0.9):
        self.choice = choice
        self.irreversible = irreversible
        self.pending_text = None
        self.replay_plan = {"steps": []}
        self.state = {
            "page": initial_page,
            "status": "ready",
            "outcome": None,
            "decision": None,
            "history": [],
            "decisions": [],
            "started_at": None,
            "progress_at": time.monotonic(),
            "stall_rounds": 0,
            "recent_fingerprints": [],
            "replay": {"available": True},
        }
        self.closed = False
        self.acts = 0
        self.values = {}
        self.private_values = set()

    def supplied_text(self, action):
        return self.values.get(action.get("label"))

    def command(self, name, body=None):
        if name == "predict":
            self.state["started_at"] = self.state["started_at"] or time.perf_counter()
            self.state["decision"] = {
                "choice": self.choice,
                "irreversible": self.irreversible,
            }
            self.state["decisions"].append(self.state["decision"])
            self.state["status"] = "predicted"
        elif name == "act":
            assert body["fingerprint"] == self.state["page"]["fingerprint"]
            self.acts += 1
            self.state["history"].append({"kind": "click", "outcome": "navigated"})
            self.state["status"] = "done"
            self.state["outcome"] = "verified"
        else:
            raise AssertionError(name)
        return self.state

    def stop_budget(self, reason):
        self.state["status"] = "budget_exhausted"
        self.state["outcome"] = "budget_exhausted"

    def note_progress(self, observed):
        self.state["progress_at"] = time.monotonic()

    def close(self):
        self.closed = True


def test_login_resume_then_exact_action_approval_uses_one_tab():
    publish = {
        "id": "e1", "kind": "click", "role": "button",
        "label": "Publish site", "form_submit": True,
    }
    browser = FakeBrowser(page(action=publish, fingerprint="after-login"))
    agent = FakeAgent(page(password=True), "e1")
    task = DelegatedTask(agent, browser)

    login = task.drive()
    assert login["phase"] == "needs_human" and login["reason"] == "login"
    assert login["actions"] == 0
    assert agent.replay_plan is None
    approval = task.resume(login["checkpoint"])
    assert browser.observations == 1
    assert approval["phase"] == "needs_human"
    assert approval["reason"] == "consequential_action"
    assert approval["proposed_action"]["label"] == "Publish site"
    assert agent.acts == 0
    with pytest.raises(ValueError, match="Stale"):
        task.resume(login["checkpoint"], approve_action=True)
    assert task.resume(approval["checkpoint"])["checkpoint"] == approval["checkpoint"]
    done = task.resume(approval["checkpoint"], approve_action=True)
    assert done["phase"] == "complete" and done["outcome"] == "verified"
    assert agent.acts == 1
    assert browser.observations == 2
    assert browser.closed and agent.closed


def test_approval_is_discarded_when_page_changes_during_human_wait():
    publish = {
        "id": "e1", "kind": "click", "role": "button",
        "label": "Publish site", "form_submit": True,
    }
    browser = FakeBrowser(page(action=publish, fingerprint="new-page"))
    agent = FakeAgent(page(action=publish), "e1")
    task = DelegatedTask(agent, browser)
    approval = task.drive()
    assert approval["phase"] == "needs_human"

    next_approval = task.resume(approval["checkpoint"], approve_action=True)
    assert agent.acts == 0
    assert next_approval["phase"] == "needs_human"
    assert next_approval["checkpoint"] != approval["checkpoint"]


def test_cancelled_checkpoint_does_not_act_and_releases_registry():
    browser = FakeBrowser(page())
    agent = FakeAgent(page(password=True), "e1")
    task = DelegatedTask(agent, browser)
    registry = TaskRegistry()
    registry.add(task)
    checkpoint = task.drive()
    assert registry.get(task.id) is task
    cancelled = task.cancel()
    registry.release_if_terminal(task)
    assert cancelled["phase"] == "cancelled"
    assert registry.current is None
    assert browser.closed and agent.acts == 0
    with pytest.raises(ValueError, match="Unknown"):
        registry.get(task.id)
    assert checkpoint["checkpoint"] == 1


def test_abandoned_setup_cannot_register_an_orphan_task():
    registry = TaskRegistry()
    reservation = registry.reserve()
    registry.abandon_active()
    task = DelegatedTask(FakeAgent(page(), "e1"), FakeBrowser(page()))
    with pytest.raises(ValueError, match="abandoned"):
        registry.add(task, reservation=reservation)
    registry.release_reservation(reservation)
    assert registry.current is None


def test_terminal_decision_reaches_agent_without_an_element():
    browser = FakeBrowser(page())
    agent = FakeAgent(page(), "DONE")
    task = DelegatedTask(agent, browser)
    result = task.drive()
    assert result["phase"] == "complete" and agent.acts == 1


def test_approval_boundary_covers_submit_and_security_but_not_selects():
    assert approval_boundary({}, {"kind": "click", "role": "button", "label": "Save", "form_submit": True})
    assert approval_boundary({}, {"kind": "click", "role": "button", "label": "Change password"})
    assert approval_boundary({}, {"kind": "key", "key": "Enter"}) == "submit_key"
    assert approval_boundary({}, {"kind": "select", "label": "Theme"}) is None


def test_profile_fill_requires_origin_approval_once(tmp_path):
    import json

    from AgentJevUltraFast.profile import ProfileStore

    (tmp_path / "identity.json").write_text(
        json.dumps({"fields": {"Email": "ada@example.test", "Password": "not-stored"}}),
        encoding="utf-8",
    )
    profile = ProfileStore(tmp_path)
    fill = {"id": "e1", "kind": "fill", "role": "textbox", "label": "Email"}
    browser = FakeBrowser(page(action=fill))
    agent = FakeAgent(page(action=fill), "e1")
    task = DelegatedTask(agent, browser, profile=profile)
    prompt = task.drive()
    assert prompt["reason"] == "confirm_fill" and agent.acts == 0
    assert prompt["proposed_action"]["source"] == "local_profile"
    done = task.resume(prompt["checkpoint"], approve_action=True)
    assert done["phase"] == "complete" and agent.acts == 1
    assert "ada@example.test" in agent.private_values
    assert agent.values == {} and agent.profile_fill is None
    assert profile.value_for("https://example.test/setup", "Password") is None
    assert ProfileStore(tmp_path).is_approved("https://example.test/another")


def test_completed_task_surfaces_gate_refusals_for_promotion():
    step = {
        "id": "e1", "kind": "click", "role": "button", "label": "Continue",
    }
    browser = FakeBrowser(page(action=step, fingerprint="p1"))
    agent = FakeAgent(page(action=step), "e1", irreversible=0.1)
    agent.state["history"].append({
        "kind": "click", "action": "Delete account", "skipped": True,
        "recovery": "skipped: below the confidence floor", "outcome": "action failed",
    })
    task = DelegatedTask(agent, browser)
    result = task.drive()
    assert result["phase"] == "complete"
    assert result["refusals"] == [{
        "kind": "click", "label": "Delete account", "reason": "below the confidence floor",
    }]
    assert result["gate"] is None and result["stall"] is None


def test_mcp_server_sets_bu_name_before_browser_harness_loads():
    """browser_harness reads BU_NAME at import time.

    Regression: a top-level `delegation` import in jev_mcp_server.py imported browser.py
    (and browser_harness) before BU_NAME was set, so every MCP server shared the "default"
    daemon and tab -- the documented "silently wrong pages" failure.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key != "BU_NAME"}
    env["JEV_MCP_BU_NAME"] = "jev-bu-order-test"
    script = (
        "import jev_mcp_server, browser_harness.admin as admin, os; "
        "print(os.environ.get('BU_NAME'), admin.NAME)"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "jev-bu-order-test jev-bu-order-test"


def test_delegate_fixture_route_exposes_the_checkpoint_flow():
    from urllib.request import urlopen

    from scripts.trap_server import serve

    server = serve(0)
    try:
        port = server.server_address[1]
        html = urlopen(f"http://127.0.0.1:{port}/delegate", timeout=5).read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert 'type="password"' in html
    assert 'id="sign-in"' in html
    assert 'id="workspace" hidden' in html
    assert "Publish demo site" in html


def test_mcp_task_tools_keep_the_same_session_across_login_and_approval(monkeypatch):
    import json

    import AgentJevUltraFast
    import AgentJevUltraFast.browser as browser_module
    import jev_mcp_server

    publish = {
        "id": "e1", "kind": "click", "role": "button",
        "label": "Publish site", "form_submit": True,
    }
    browsers = []

    def make_browser(url, *, follow_tabs=None):
        assert follow_tabs is False
        browser = FakeBrowser(page(action=publish, fingerprint="after-login"))
        browsers.append(browser)
        return browser

    monkeypatch.setattr(browser_module, "Browser", make_browser)
    monkeypatch.setattr(AgentJevUltraFast, "Agent", lambda url, goal, **kwargs:
                        FakeAgent(page(password=True), "e1"))
    monkeypatch.setattr(jev_mcp_server, "_TASKS", TaskRegistry())
    monkeypatch.setattr(jev_mcp_server, "_load_env", lambda: None)
    monkeypatch.setattr(jev_mcp_server, "_drop_warm", lambda: None)
    monkeypatch.setattr(jev_mcp_server, "_evidence_dir", lambda: None)

    def call(name, arguments):
        reply = jev_mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        assert reply["result"]["isError"] is False
        return json.loads(reply["result"]["content"][0]["text"])

    first = call("browser_task", {"url": "https://example.test/setup", "goal": "Set up the site"})
    assert first["reason"] == "login"
    second = call("browser_task_resume", {
        "task_id": first["task_id"], "checkpoint": first["checkpoint"],
    })
    assert second["reason"] == "consequential_action"
    done = call("browser_task_resume", {
        "task_id": second["task_id"], "checkpoint": second["checkpoint"], "approve_action": True,
    })
    assert done["phase"] == "complete" and done["outcome"] == "verified"
    assert len(browsers) == 1 and browsers[0].observations == 2
    assert jev_mcp_server._TASKS.current is None
    task_tools = {tool["name"]: tool for tool in jev_mcp_server.TOOLS}
    assert task_tools["browser_task"]["annotations"]["destructiveHint"] is True
