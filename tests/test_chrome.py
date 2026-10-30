import pytest

from AgentJevUltraFast import chrome


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("JEV_CHROME", "JEV_CHROME_PORT", "JEV_CHROME_PATH", "JEV_CHROME_HEADLESS",
                 "BU_CDP_WS", "BU_CDP_URL", "BU_NAME"):
        # set-then-delete so teardown restores the original state even for variables the
        # code under test writes straight into os.environ.
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setattr(chrome, "ENV_FILES", ())


def test_dedicated_mode_can_come_from_an_env_file(monkeypatch, tmp_path):
    import os
    env = tmp_path / ".env"
    env.write_text("TEXT_MODEL=x\nJEV_CHROME=dedicated\nJEV_CHROME_PORT='9555'\n", encoding="utf-8")
    monkeypatch.setattr(chrome, "ENV_FILES", (env,))
    chrome.apply_env()
    assert os.environ["BU_CDP_URL"] == "http://127.0.0.1:9555"
    assert "TEXT_MODEL" not in os.environ or os.environ["TEXT_MODEL"] != "x"


def test_user_mode_leaves_harness_env_alone():
    chrome.apply_env()
    import os
    assert "BU_CDP_URL" not in os.environ and "BU_NAME" not in os.environ


def test_dedicated_mode_routes_to_its_own_daemon_and_port(monkeypatch):
    import os
    monkeypatch.setenv("JEV_CHROME", "dedicated")
    monkeypatch.setenv("JEV_CHROME_PORT", "9444")
    monkeypatch.setenv("BU_NAME", "jev-check-03")
    chrome.apply_env()
    chrome.apply_env()  # idempotent: the suffix is added once
    assert os.environ["BU_CDP_URL"] == "http://127.0.0.1:9444"
    assert os.environ["BU_NAME"] == "jev-check-03-chrome9444"


def test_a_worker_inheriting_our_endpoint_still_gets_its_own_daemon_name(monkeypatch):
    # `jev test` passes its environment (BU_CDP_URL included) to each worker with a fresh
    # BU_NAME. Without the suffix the worker reused a same-named daemon on the user's Chrome.
    import os
    monkeypatch.setenv("JEV_CHROME", "dedicated")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setenv("BU_NAME", "jev-check-04")
    chrome.apply_env()
    assert os.environ["BU_NAME"] == "jev-check-04-chrome9333"


def test_someone_elses_cdp_url_keeps_its_daemon_name(monkeypatch):
    import os
    monkeypatch.setenv("JEV_CHROME", "dedicated")
    monkeypatch.setenv("BU_CDP_URL", "http://10.0.0.5:9222")
    monkeypatch.setenv("BU_NAME", "mine")
    chrome.apply_env()
    assert os.environ["BU_NAME"] == "mine" and os.environ["BU_CDP_URL"] == "http://10.0.0.5:9222"


def test_explicit_cdp_endpoint_wins(monkeypatch):
    import os
    monkeypatch.setenv("JEV_CHROME", "dedicated")
    monkeypatch.setenv("BU_CDP_WS", "ws://remote")
    chrome.apply_env()
    assert "BU_CDP_URL" not in os.environ and "BU_NAME" not in os.environ


def test_running_browser_is_reused_without_launch(monkeypatch):
    monkeypatch.setattr(chrome, "version", lambda timeout=1.0: {"Browser": "Chrome/150"})
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert chrome.ensure_chrome()["Browser"] == "Chrome/150"


def test_browser_that_never_answers_is_an_error_not_a_hang(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_CHROME_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setattr(chrome, "version", lambda timeout=1.0: None)
    monkeypatch.setattr(chrome, "find_chrome", lambda: "chrome")
    launched = []
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda command, **k: launched.append(command))
    with pytest.raises(RuntimeError, match="dedicated-chrome-timeout"):
        chrome.ensure_chrome(timeout=0.3)
    assert "--remote-debugging-port=9333" in launched[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in launched[0]


def test_missing_chrome_is_reported(monkeypatch):
    monkeypatch.setattr(chrome, "version", lambda timeout=1.0: None)
    monkeypatch.setattr(chrome, "find_chrome", lambda: None)
    with pytest.raises(RuntimeError, match="dedicated-chrome-missing"):
        chrome.ensure_chrome()


@pytest.mark.parametrize("argv, expected", [
    (["jev", "test", "suite.md"], True),
    ([r"C:\venv\Scripts\jev.exe", "draft", "http://localhost:3000"], True),
    (["-m", "test", "suite.md"], True),                       # python -m AgentJevUltraFast.cli test
    (["jev", "--brief", "open youtube"], False),              # "do X in my browser" keeps the user's Chrome
    (["jev"], False),
    (["pytest", "test"], False),
])
def test_unattended_commands_choose_the_dedicated_chrome(argv, expected):
    assert chrome.unattended_command(argv) is expected


def test_jev_test_selects_dedicated_chrome_before_any_browser_import(monkeypatch):
    import os
    monkeypatch.setattr(chrome.sys, "argv", ["jev", "test", "suite.md"])
    chrome.apply_env()
    assert os.environ["JEV_CHROME"] == "dedicated" and os.environ["BU_CDP_URL"].endswith(":9333")


def test_chrome_user_undoes_only_what_apply_env_set(monkeypatch):
    import os
    monkeypatch.setattr(chrome.sys, "argv", ["jev", "test", "suite.md"])
    monkeypatch.setenv("BU_NAME", "jev-check-01")
    chrome.apply_env()
    chrome.use_user_chrome()
    assert os.environ["JEV_CHROME"] == "user" and "BU_CDP_URL" not in os.environ
    assert os.environ["BU_NAME"] == "jev-check-01"
    monkeypatch.setenv("BU_CDP_URL", "http://10.0.0.5:9222")       # someone else's endpoint stays
    chrome.use_user_chrome()
    assert os.environ["BU_CDP_URL"] == "http://10.0.0.5:9222"
