"""browser_test starts a suite without holding the MCP call open; status reads its report."""

import json


class FakeProc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def test_browser_test_starts_in_the_background_and_reports_when_done(monkeypatch, tmp_path):
    import jev_mcp_server as server

    launched = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(server, "TEST_RUNS_DIR", tmp_path)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    started = server.tool_browser_test({
        "spec_markdown": "## Home loads\n1. Read the page.\n   - see: Welcome\n",
        "base_url": "http://localhost:3000", "workers": 2})
    assert started["status"] == "running" and started["tests"] == 1
    command = launched["command"]
    assert command[1:4] == ["-m", "AgentJevUltraFast.cli", "test"] and command[-2:] == ["--workers", "2"]
    # base_url overrides the suite for every format, through the runner's own override.
    assert launched["env"]["JEV_TRAP_BASE"] == "http://localhost:3000"

    status = server.tool_browser_test_status({"run_id": started["run_id"]})
    assert status["status"] == "running" and status["tests"] == 1

    run = server._TEST_RUNS[started["run_id"]]
    run["proc"] = FakeProc(1)
    run["report"].parent.mkdir(parents=True, exist_ok=True)
    run["report"].write_text(json.dumps({
        "passed": 0, "total": 1, "usd_estimate": 0.0004, "model_calls": 5, "steps_replayed": 0,
        "results": [{"name": "Home loads", "verified": False, "status": "done", "failed_step": 1,
                     "checks": [{"check": "text_contains_all", "pass": False, "note": "missing"}],
                     "page_excerpt": "Sign in", "cost": {"usd_estimate": 0.0004}}]}), encoding="utf-8")
    done = server.tool_browser_test_status({"run_id": started["run_id"]})
    assert done["status"] == "finished" and done["passed"] == 0 and done["model_calls"] == 5
    result = done["results"][0]
    assert result["failed_step"] == 1 and result["page_excerpt"] == "Sign in"
    assert result["failing_checks"][0]["check"] == "text_contains_all"


def test_a_malformed_spec_is_refused_before_anything_starts(monkeypatch, tmp_path):
    import jev_mcp_server as server

    monkeypatch.setattr(server, "TEST_RUNS_DIR", tmp_path)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    result = server.tool_browser_test({"spec_markdown": "## T\n1. Go.\n   - smell: roses\n"})
    assert "unknown check" in result["error"]
