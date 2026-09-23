"""The parallel check harness must always return, and must name a setup failure as one."""

import json
import subprocess

import AgentJevUltraFast
from scripts import run_checks


def test_a_worker_past_its_bound_is_killed_and_reported(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                        stderr=b"handshake-wait: click Allow")

    monkeypatch.setattr(run_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(run_checks, "STARTUP_GRACE_S", 5.0)
    proc = run_checks.run_job({"max_seconds": 10, "name": "x"}, "part.json", "jev-check-00")
    assert seen["timeout"] == 15.0
    assert proc.returncode is None
    assert "killed after 15s" in proc.stderr
    assert "Allow remote debugging" in proc.stderr


def test_a_killed_worker_without_a_prompt_does_not_blame_chrome(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=None)

    monkeypatch.setattr(run_checks.subprocess, "run", fake_run)
    proc = run_checks.run_job({"max_seconds": 1}, "part.json", "jev-check-01")
    assert proc.returncode is None
    assert "Allow remote debugging" not in proc.stderr


def test_an_unapproved_browser_connection_is_blocked_not_an_app_failure(monkeypatch, tmp_path):
    class Refused:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("permission-blocked: the pending Chrome connection ended "
                               "before approval")

    monkeypatch.setattr(run_checks, "load_env", lambda: None)
    monkeypatch.setattr(AgentJevUltraFast, "Agent", Refused)
    out = tmp_path / "part.json"
    run_checks.run_worker({"name": "n", "url": "http://127.0.0.1:1/", "goal": "g"}, out)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "blocked"
    assert result["blocked_reason"] == "chrome-approval"
    assert "Allow remote debugging" in result["error"]


def test_any_other_worker_error_names_where_it_was_raised(monkeypatch, tmp_path):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(run_checks, "load_env", lambda: None)
    monkeypatch.setattr(AgentJevUltraFast, "Agent", Broken)
    out = tmp_path / "part.json"
    run_checks.run_worker({"name": "n", "url": "http://127.0.0.1:1/", "goal": "g"}, out)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["error"].startswith("OSError")
    assert result["error_at"].startswith("test_run_checks.py:")
