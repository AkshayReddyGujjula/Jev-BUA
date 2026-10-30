"""The `jev` front door: no model between the user's words and Jev."""

import builtins

from AgentJevUltraFast import cli, start, tui


def test_explicit_urls_and_domains_are_used_without_a_lookup(monkeypatch):
    monkeypatch.setattr(start, "first_result", lambda phrase: pytest_fail(phrase))
    assert start.resolve("visit https://example.com/a?b=1 and read it") == ("https://example.com/a?b=1", "explicit")
    assert start.resolve("book a flight on flights.google.com")[0] == "https://flights.google.com"
    assert start.resolve("use deepseek v4.1 e.g. to summarise") == (None, "none")


def pytest_fail(phrase):
    raise AssertionError(f"looked up {phrase!r}")


def test_the_site_phrase_is_the_words_after_the_opening_verb():
    assert start.site_phrase("Open YouTube, type in MKBHD") == "YouTube"
    assert start.site_phrase("can you open studycanvas, go inside the workspace") == "studycanvas"
    assert start.site_phrase("go to my gmail and find the invoice") == "gmail"
    assert start.site_phrase("search for cheap flights") is None
    assert start.site_phrase("turn on captions") is None


def test_interactive_reuses_one_tab_and_continues_on_the_current_page(monkeypatch):
    lines = iter(["open youtube, search mkbhd", "turn on captions", ":quit"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(lines))
    monkeypatch.setattr(start, "first_result", lambda phrase: "https://youtube.com/")
    made, runs = [], []

    class FakeBrowser:
        def __init__(self, url):
            made.append(url)

        def navigate(self, url):
            raise AssertionError("a follow-up with no site must not navigate")

        def evaluate(self, _expression):
            return "https://www.youtube.com/watch?v=1"

    import AgentJevUltraFast.browser as browser

    monkeypatch.setattr(browser, "Browser", FakeBrowser)

    def fake_run_once(args, browser=None, on_state=None):
        runs.append((args.goal, args.url, browser))
        return [{"status": "done", "outcome": "unknown", "actions": 1}], None, 0

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    assert tui.interactive(tab="background") == 0
    assert made == ["https://youtube.com/"]                      # one tab for the session
    assert runs[0][:2] == ("open youtube, search mkbhd", "https://youtube.com/")
    assert runs[1][:2] == ("turn on captions", "https://www.youtube.com/watch?v=1")
    assert runs[0][2] is runs[1][2]
