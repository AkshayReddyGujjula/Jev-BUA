"""Offline lifecycle tests for the opt-in Browser Use Cloud V4 launcher.

A fake SDK client and a fake child process stand in for the real BrowserUse client and the
Jev CLI: no browser, no network, no paid call. The verdicts are the recorded stop list, the
child's environment and the recovery file - read from the launcher's behavior, not its word.
"""

import io
import json
import os
import subprocess
import sys

import pytest

from AgentJevUltraFast import cloud_runner

GOAL = "open example.com and read the headline"
URL = "https://example.com/start"
KEY = "cloud-test-key-never-printed"
CDP = "ws://cloud.test/devtools/browser/session-42"
RECORD = ("cloud", "browser.json")


class FakeSession:
    def __init__(self, session_id="cloud-session-42", cdp_url=CDP):
        self.id = session_id
        self.cdp_url = cdp_url


class FakeBrowsers:
    def __init__(self, session=None, create_error=None, stop_error=None):
        self.session = session
        self.create_error = create_error
        self.stop_error = stop_error
        self.create_kwargs = []
        self.stopped = []
        self.stop_hook = None

    def create(self, **kwargs):
        self.create_kwargs.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return self.session

    def stop(self, session_id):
        if self.stop_hook is not None:
            self.stop_hook(session_id)
        self.stopped.append(session_id)
        if self.stop_error is not None:
            raise self.stop_error
        return {"stopped": session_id}


class FakeClient:
    def __init__(self, browsers):
        self.browsers = browsers
        self.closed = False

    def close(self):
        self.closed = True


def base_env():
    """A local environment with the one key the launcher requires and no developer secrets."""
    return {"BROWSER_USE_API_KEY": KEY, "PATH": os.environ.get("PATH", "")}


def run_launcher(monkeypatch, tmp_path, browsers, *, returncode=0, timeout=False, environ=None,
                 proxy=None, profile="", timeout_s=300.0, err=None, daemon_stopper=None,
                 client=None):
    """Drive `run_cloud` with a fake child; returns the launcher code and the child calls."""
    calls = []
    record = tmp_path.joinpath(*RECORD)

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    code = cloud_runner.run_cloud(
        GOAL, URL, proxy=proxy, profile=profile, timeout_s=timeout_s, record_path=record,
        environ=base_env() if environ is None else environ,
        client=client if client is not None else FakeClient(browsers),
        daemon_stopper=daemon_stopper if daemon_stopper is not None else (lambda name: None),
        err=err if err is not None else io.StringIO(),
    )
    return code, calls


def test_a_successful_child_run_stops_the_exact_created_browser(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession())
    record = tmp_path.joinpath(*RECORD)
    seen = {}

    def fake_run(command, **kwargs):
        # The recovery record must already exist, and hold the browser ID, before the child.
        seen["record_text"] = record.read_text(encoding="utf-8")
        seen["command"], seen["env"] = command, kwargs["env"]
        seen["cwd"], seen["timeout"] = kwargs["cwd"], kwargs["timeout"]
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    err = io.StringIO()
    code = cloud_runner.run_cloud(GOAL, URL, environ=base_env(), client=FakeClient(browsers),
                                  record_path=record, daemon_stopper=lambda name: None, err=err)

    assert code == 0
    assert browsers.stopped == ["cloud-session-42"]           # the exact created browser ID
    assert browsers.create_kwargs == [{"proxy_country_code": None, "timeout": 5}]
    assert json.loads(seen["record_text"])["session_id"] == "cloud-session-42"
    assert CDP not in seen["record_text"] and KEY not in seen["record_text"]
    command = seen["command"]
    assert command[:2] == [sys.executable, str(cloud_runner.REPO / "run_task.py")]
    assert command[command.index("--goal") + 1] == GOAL
    assert command[command.index("--url") + 1] == URL
    assert command[command.index("--max-seconds") + 1] == "270"   # the launcher's bound, minus grace
    assert CDP not in command and KEY not in command
    assert seen["env"]["BU_NAME"].endswith("cloud-session-42")
    assert seen["env"]["BU_CDP_WS"] == CDP
    assert seen["env"][cloud_runner.API_KEY_ENV] == ""        # blocks CLI .env reload
    assert seen["cwd"] == str(cloud_runner.REPO)
    assert seen["timeout"] == 300.0
    assert seen["kwargs"]["capture_output"] is True
    assert not record.exists()                                # nothing left to recover
    text = err.getvalue()
    assert KEY not in text and CDP not in text
    assert "cloud-session-42" in text and "stopped" in text


def test_a_failed_child_run_still_stops_the_exact_created_browser(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession())
    code, calls = run_launcher(monkeypatch, tmp_path, browsers, returncode=2,
                               err=io.StringIO())
    assert code == 2
    assert len(calls) == 1
    assert browsers.stopped == ["cloud-session-42"]


def test_a_child_past_its_budget_is_killed_and_the_browser_still_stops(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession())
    err = io.StringIO()
    code, calls = run_launcher(monkeypatch, tmp_path, browsers, timeout=True, timeout_s=5.0,
                               err=err)
    assert code == 2
    assert calls[0]["timeout"] == 5.0
    assert browsers.stopped == ["cloud-session-42"]
    assert "killed" in err.getvalue()


def test_a_missing_api_key_starts_no_browser_and_no_child(monkeypatch, tmp_path):
    built, calls = [], []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    err = io.StringIO()

    def factory(api_key):
        built.append(api_key)
        raise AssertionError("no client may be built without a key")

    for environ in ({}, {"BROWSER_USE_API_KEY": "   "}):
        browsers = FakeBrowsers(FakeSession())
        # A working client object is available; the missing key alone must keep it unused.
        code = cloud_runner.run_cloud(GOAL, URL, environ=environ, client=FakeClient(browsers),
                                      client_factory=factory, record_path=tmp_path.joinpath(*RECORD),
                                      daemon_stopper=lambda name: None, err=err)
        assert code == 2
        assert browsers.create_kwargs == [] and browsers.stopped == []
    assert built == [] and calls == []
    assert not tmp_path.joinpath(*RECORD).exists()
    assert "BROWSER_USE_API_KEY" in err.getvalue()


def test_a_failed_stop_is_reported_without_the_key_or_the_cdp_url(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession(), stop_error=RuntimeError(f"refused {CDP} key={KEY}"))
    err = io.StringIO()
    code, _ = run_launcher(monkeypatch, tmp_path, browsers, err=err)

    assert code == 1                          # the child succeeded; the failed stop turns it red
    assert browsers.stopped == ["cloud-session-42"]
    text = err.getvalue()
    assert KEY not in text and CDP not in text
    assert "[redacted]" in text and "stop failed" in text and "cloud-session-42" in text
    # The recovery record survives a failed stop so the browser can be stopped by hand.
    record = json.loads(tmp_path.joinpath(*RECORD).read_text(encoding="utf-8"))
    assert record["session_id"] == "cloud-session-42"
    assert CDP not in json.dumps(record) and KEY not in json.dumps(record)


def test_the_proxy_choice_reaches_create_and_none_sends_no_proxy_argument(monkeypatch, tmp_path):
    with_proxy = FakeBrowsers(FakeSession())
    code, _ = run_launcher(monkeypatch, tmp_path, with_proxy, proxy="DE")
    assert code == 0
    assert with_proxy.create_kwargs == [{"proxy_country_code": "de", "timeout": 5}]

    without = FakeBrowsers(FakeSession())
    code, _ = run_launcher(monkeypatch, tmp_path, without, proxy=None)
    assert code == 0
    assert without.create_kwargs == [{"proxy_country_code": None, "timeout": 5}]


def test_a_cloud_profile_is_passed_to_the_verified_sdk_argument(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession())
    err = io.StringIO()
    code, calls = run_launcher(monkeypatch, tmp_path, browsers, profile="profile-7", err=err)
    assert code == 0
    assert browsers.create_kwargs == [{"proxy_country_code": None, "timeout": 5,
                                       "profile_id": "profile-7"}]
    assert browsers.stopped == ["cloud-session-42"] and len(calls) == 1


def test_a_session_without_a_cdp_url_is_stopped_and_no_child_starts(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession(cdp_url=None))
    err = io.StringIO()
    code, calls = run_launcher(monkeypatch, tmp_path, browsers, err=err)
    assert code == 1
    assert browsers.stopped == ["cloud-session-42"]
    assert calls == []
    assert "no CDP URL" in err.getvalue()


def test_a_create_failure_runs_nothing(monkeypatch, tmp_path):
    browsers = FakeBrowsers(create_error=RuntimeError(f"rejected key {KEY}"))
    err = io.StringIO()
    code, calls = run_launcher(monkeypatch, tmp_path, browsers, err=err)
    assert code == 1
    assert browsers.stopped == [] and calls == []
    assert KEY not in err.getvalue()


def test_a_client_failure_is_scrubbed_and_starts_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    err = io.StringIO()

    def factory(_api_key):
        raise RuntimeError(f"bad key {KEY}")

    code = cloud_runner.run_cloud(GOAL, URL, environ=base_env(), client_factory=factory,
                                  record_path=tmp_path.joinpath(*RECORD),
                                  daemon_stopper=lambda name: None, err=err)
    assert code == 1
    assert calls == []
    assert KEY not in err.getvalue() and "[redacted]" in err.getvalue()


def test_the_child_daemon_is_stopped_before_the_cloud_browser(monkeypatch, tmp_path):
    events = []
    browsers = FakeBrowsers(FakeSession())
    browsers.stop_hook = lambda session_id: events.append(("cloud", session_id))

    def stop_daemon(name):
        events.append(("daemon", name))
        raise RuntimeError("daemon stop unavailable")

    err = io.StringIO()
    code, _ = run_launcher(monkeypatch, tmp_path, browsers, daemon_stopper=stop_daemon, err=err)

    assert code == 0                          # the daemon failure is ignored, the run's code stays
    assert [kind for kind, _ in events] == ["daemon", "cloud"]
    assert events[0][1].startswith("jev-cloud-")
    assert events[0][1].endswith("cloud-session-42")
    assert browsers.stopped == ["cloud-session-42"]


def test_the_default_recovery_path_is_the_ignored_artifacts_tree():
    path = cloud_runner.default_record_path()
    assert path == cloud_runner.DATA / "cloud" / "browser.json"
    assert "artifacts" in str(path) or cloud_runner.DATA == cloud_runner.HOME


def test_existing_recovery_record_blocks_another_create(monkeypatch, tmp_path):
    browsers = FakeBrowsers(FakeSession())
    record = tmp_path.joinpath(*RECORD)
    record.parent.mkdir(parents=True)
    record.write_text('{"session_id":"possibly-active"}', encoding="utf-8")
    code, calls = run_launcher(monkeypatch, tmp_path, browsers)
    assert code == 2 and calls == []
    assert browsers.create_kwargs == [] and browsers.stopped == []
    assert "possibly-active" in record.read_text(encoding="utf-8")


def test_child_output_is_scrubbed_and_sdk_client_is_closed(monkeypatch, tmp_path, capsys):
    browsers = FakeBrowsers(FakeSession())
    client = FakeClient(browsers)

    def fake_run(command, **kwargs):
        assert command[command.index("--expect") + 1] == '{"title_contains":"Example"}'
        return subprocess.CompletedProcess(command, 0,
                                           stdout=f"result contains {CDP}",
                                           stderr=f"diagnostic contains {KEY}")

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    err = io.StringIO()
    code = cloud_runner.run_cloud(GOAL, URL, expect='{"title_contains":"Example"}',
                                  environ=base_env(), client=client,
                                  record_path=tmp_path.joinpath(*RECORD),
                                  daemon_stopper=lambda name: None, err=err)
    assert code == 0 and client.closed
    output = capsys.readouterr().out + err.getvalue()
    assert CDP not in output and KEY not in output
    assert output.count("[redacted]") == 2


def test_http_cdp_address_is_resolved_to_websocket_without_logging_it(monkeypatch, tmp_path):
    http_url = "https://cloud.test/cdp/session-secret"
    discovered_ws = "wss://cloud.test/devtools/browser/resolved-secret"
    browsers = FakeBrowsers(FakeSession(cdp_url=http_url))
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(url, timeout):
        seen["url"] = url
        return Response(json.dumps({"webSocketDebuggerUrl": discovered_ws}).encode())

    def fake_run(command, **kwargs):
        seen["child_ws"] = kwargs["env"]["BU_CDP_WS"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    err = io.StringIO()
    code = cloud_runner.run_cloud(GOAL, URL, environ=base_env(), client=FakeClient(browsers),
                                  record_path=tmp_path.joinpath(*RECORD),
                                  daemon_stopper=lambda name: None, err=err)
    assert code == 0 and seen["child_ws"] == discovered_ws
    assert seen["url"] == http_url + "/json/version"
    assert browsers.stopped == ["cloud-session-42"]
    assert http_url not in err.getvalue() and discovered_ws not in err.getvalue()


def test_main_forwards_goal_url_proxy_timeout_and_record(monkeypatch, tmp_path):
    seen = {}

    def fake_run_cloud(goal, url="", **kwargs):
        seen.update({"goal": goal, "url": url, **kwargs})
        return 0

    monkeypatch.setattr(cloud_runner, "run_cloud", fake_run_cloud)
    code = cloud_runner.main(["--goal", GOAL, "--url", URL, "--proxy", "DE",
                              "--timeout-seconds", "12", "--record", str(tmp_path / "r.json")])
    assert code == 0
    assert seen["goal"] == GOAL and seen["url"] == URL
    assert seen["proxy"] == "de" and seen["profile"] == ""
    assert seen["timeout_s"] == 12.0
    assert seen["record_path"] == tmp_path / "r.json"


def test_main_rejects_a_proxy_that_is_neither_none_nor_a_country_code():
    with pytest.raises(SystemExit) as exited:
        cloud_runner.main(["--goal", GOAL, "--proxy", "usa"])
    assert exited.value.code == 2
