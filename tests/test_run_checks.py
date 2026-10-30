"""The parallel check harness must always return, and must name a setup failure as one."""

import json
import subprocess

import AgentJevUltraFast
from AgentJevUltraFast import checks as run_checks


class FakeBrowser:
    """Stands in for the real tab: a unit test must never open a browser connection."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    def evaluate(self, expression):
        return {}

    def close(self):
        self.closed = True


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
    monkeypatch.setattr(AgentJevUltraFast, "Browser", FakeBrowser)
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
    monkeypatch.setattr(AgentJevUltraFast, "Browser", FakeBrowser)
    out = tmp_path / "part.json"
    run_checks.run_worker({"name": "n", "url": "http://127.0.0.1:1/", "goal": "g"}, out)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["error"].startswith("OSError")
    assert result["error_at"].startswith("test_run_checks.py:")


def test_steps_share_one_tab_and_stop_at_the_first_failure(monkeypatch, tmp_path):
    seen = []

    class StepAgent:
        def __init__(self, url, goal, **kwargs):
            seen.append((goal, kwargs["browser"], kwargs.get("values")))
            self.goal = goal

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self):
            return iter(())

        def snapshot(self):
            ok = self.goal != "delete it"
            return {"status": "done", "outcome": "verified" if ok else "refuted",
                    "checks": [], "history": [{"action": "x"}], "decisions": [{"usage": {}}],
                    "text_calls": [], "page": {}, "replay": {"used": 0}}

    monkeypatch.setattr(run_checks, "load_env", lambda: None)
    monkeypatch.setattr(AgentJevUltraFast, "Agent", StepAgent)
    monkeypatch.setattr(AgentJevUltraFast, "Browser", FakeBrowser)
    out = tmp_path / "part.json"
    job = {"name": "n", "url": "http://app.test/", "base_url": "http://app.test",
           "values": {"Name": "Ada"},
           "steps": [{"goal": "sign in", "expect": {"text_contains": ["Home"]}},
                     {"goal": "delete it", "expect": {"text_absent": ["Draft"]}},
                     {"goal": "never reached", "expect": {"text_contains": ["x"]}}]}
    run_checks.run_worker(job, out)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert [goal for goal, _, _ in seen] == ["sign in", "delete it"]
    assert seen[0][1] is seen[1][1]          # one browser (one tab, one context) for every step
    assert seen[0][2] == seen[1][2] == {"Name": "Ada"}  # check-level values reach every step
    assert result["verified"] is False and result["failed_step"] == 2
    assert result["decisions"] == 2 and result["cost"]["calls"]["jev"] == 2


def test_only_local_dev_hosts_replay_commits_by_default():
    assert run_checks.local_host("http://localhost:5173/x")
    assert run_checks.local_host("http://127.0.0.1:8000/")
    assert run_checks.local_host("http://app.test/")
    assert not run_checks.local_host("https://staging.example.com/")
    assert not run_checks.local_host("https://localhost.evil.com/")


def test_a_step_whose_readback_failed_fails_the_check(monkeypatch, tmp_path):
    class Unknown:
        def __init__(self, url, goal, **kwargs):
            self.goal = goal

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self):
            return iter(())

        def snapshot(self):
            # A failed readback: checks stay None and the outcome is "unknown", not "refuted".
            outcome = "unknown" if self.goal == "first" else "verified"
            return {"status": "done", "outcome": outcome, "checks": None, "history": [],
                    "decisions": [], "text_calls": [], "page": {}, "replay": {}}

    monkeypatch.setattr(run_checks, "load_env", lambda: None)
    monkeypatch.setattr(AgentJevUltraFast, "Agent", Unknown)
    monkeypatch.setattr(AgentJevUltraFast, "Browser", FakeBrowser)
    out = tmp_path / "part.json"
    run_checks.run_worker({"name": "n", "url": "http://app.test/", "base_url": "http://app.test",
                           "steps": [{"goal": "first", "expect": {"text_contains": ["A"]}},
                                     {"goal": "second", "expect": {"text_contains": ["B"]}}]}, out)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["verified"] is False and result["failed_step"] == 1


def _fake_suite(tmp_path, monkeypatch, setup_ok):
    spec_file = tmp_path / "suite.md"
    spec_file.write_text("base_url: http://localhost:3000\nisolate: storage\n\n"
                         "## Before all\n1. Sign in.\n   - see: Home\n\n"
                         "## A\n1. Go.\n   - see: A\n\n## B\n1. Go.\n   - see: B\n", encoding="utf-8")
    ran = []

    def fake_run_job(job, part, bu_name):
        ran.append((job["name"], job.get("isolate")))
        ok = setup_ok if job["name"] == "Before all" else True
        from pathlib import Path
        Path(part).write_text(json.dumps({"name": job["name"], "status": "done", "verified": ok,
                                          "checks": [], "wall_s": 0.1}), encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(run_checks, "run_job", fake_run_job)
    monkeypatch.setenv("JEV_CHROME", "user")
    out = tmp_path / "report.json"
    args = run_checks.argparse.Namespace(checks=str(spec_file), workers=4, out=str(out), replay=None,
                                         no_evidence=True, state_dir=str(tmp_path))
    code = run_checks.run_parent(args)
    return code, ran, json.loads(out.read_text(encoding="utf-8"))


def test_before_all_runs_first_and_tests_keep_its_state(tmp_path, monkeypatch):
    code, ran, report = _fake_suite(tmp_path, monkeypatch, setup_ok=True)
    assert ran == [("Before all", "storage"), ("A", None), ("B", None)]
    assert code == 0 and report["passed"] == 2 and report["total"] == 2


def test_a_failed_before_all_blocks_every_test(tmp_path, monkeypatch):
    code, ran, report = _fake_suite(tmp_path, monkeypatch, setup_ok=False)
    assert ran == [("Before all", "storage")]
    assert code == 1 and report["passed"] == 0
    assert all("Before all did not verify" in r["error"] for r in report["results"])


def test_a_step_without_checks_gets_suggestions_from_what_it_changed():
    from AgentJevUltraFast.checks import suggest_checks
    before = "Home\nNew canvas\n12:04\nNo canvases yet\nSearch"
    after = "Home\nNew canvas\n12:05\nBiology Chapter 3\nSaved for ada@example.com\nSearch"
    suggested = suggest_checks(before, after, private=("ada@example.com",))
    # The clock is not a check, and a line holding a private value is dropped, not redacted.
    assert suggested == ["see: Biology Chapter 3", "not see: No canvases yet"]


def test_isolate_is_normalised_the_same_everywhere():
    from AgentJevUltraFast.checks import isolation
    assert [isolation(v) for v in (None, False, "none", "off", "", True, "context", "storage", "Storage")] == \
        [None, None, None, None, None, "context", "context", "storage", "storage"]


def test_suggestions_stay_fast_on_a_huge_page():
    import time

    from AgentJevUltraFast.checks import suggest_checks
    before = "\n".join(f"row {i} item" for i in range(40_000))
    started = time.perf_counter()
    suggest_checks(before, before + "\nSaved", limit=5)
    assert time.perf_counter() - started < 1.0
