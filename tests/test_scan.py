"""Caller-directed scanning: scroll and observe in code until a literal target appears.

Offline - Browser.act/observe are stubbed, so no CDP and no paid APIs.
"""

import jev_ultrafast.browser as browser

SCROLL = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}


def page(*labels, text="start", with_scroll=True):
    actions = [SCROLL] if with_scroll else []
    actions += [
        {"id": f"v{i}", "kind": "click", "label": label, "role": "link", "node": 100 + i}
        for i, label in enumerate(labels)
    ]
    return {"url": "https://example.test/list", "title": "List", "text": text, "actions": actions}


def stub(pages):
    """A Browser whose only job is to hand back a sequence of observed pages."""
    b = browser.Browser.__new__(browser.Browser)
    seen_calls = []

    def act(action, current, text=None):
        seen_calls.append(action["id"])
        return {"executed": action["id"]}

    queue = list(pages)
    b.act = act
    b.observe = lambda screenshot=True, deadline=None: queue.pop(0)
    b.seen_calls = seen_calls
    return b


def test_target_visible_matches_labels_and_text():
    assert browser.target_visible(page("Dude Perfect S1"), "dude perfect")
    assert browser.target_visible(page("Other", text="about Dude Perfect"), "dude perfect")
    assert not browser.target_visible(page("Other"), "dude perfect")
    assert not browser.target_visible(page("Other"), "")


def test_scan_stops_the_moment_the_target_appears():
    b = stub([page("Other video"), page("Dude Perfect S1E1"), page("never read")])
    out, screens, found = b.scan(SCROLL, "Dude Perfect", page("First video"))
    assert found is True
    assert screens == 2                       # read two screens, then stopped - not three
    assert any(a["label"].startswith("Dude Perfect") for a in out["actions"])


def test_scan_stops_after_several_barren_scrolls():
    """Exhaustion needs several unproductive steps, not one."""
    same = page("First video")
    b = stub([same] * (browser.SCAN_BARREN_LIMIT + 2))
    out, screens, found = b.scan(SCROLL, "nonexistent", page("First video"))
    assert found is False
    assert screens == browser.SCAN_BARREN_LIMIT


def test_a_single_barren_scroll_does_not_end_the_scan():
    """Measured: the first scan of a run aborted after two screens on a page that then yielded
    the target five screens later. A page that is still settling looks like the end of a list."""
    same = page("First video")
    productive = page("First video", "Later item")
    quiet = page("Later item")
    b = stub([same, productive] + [quiet] * browser.SCAN_BARREN_LIMIT)
    out, screens, found = b.scan(SCROLL, "nonexistent", page("First video"))
    assert screens == 2 + browser.SCAN_BARREN_LIMIT


def test_scan_stops_when_no_scroll_is_offered():
    b = stub([])
    out, screens, found = b.scan(SCROLL, "anything", page("Only item", with_scroll=False))
    assert found is False
    assert screens == 0                       # end of list: never scrolled
    assert b.seen_calls == []


def test_scan_is_a_noop_when_the_target_is_already_visible():
    b = stub([])
    out, screens, found = b.scan(SCROLL, "Dude Perfect", page("Dude Perfect S1"))
    assert found is True
    assert screens == 0
    assert b.seen_calls == []


def test_scan_respects_its_screen_bound():
    rising = [page(f"item {i}") for i in range(50)]
    b = stub(rising)
    out, screens, found = b.scan(SCROLL, "never", page("item start"), max_screens=5)
    assert found is False
    assert screens == 5


def test_scan_respects_a_deadline():
    import time

    rising = [page(f"item {i}") for i in range(50)]
    b = stub(rising)
    out, screens, found = b.scan(SCROLL, "never", page("item start"), deadline=time.monotonic() - 1)
    assert screens == 0                       # already past the deadline: hands straight back


def test_scan_rechecks_a_progress_deadline_after_each_observation():
    import time

    b = stub([page("second"), page("third"), page("never read")])
    seen = []

    def deadline():
        return time.monotonic() + 1 if len(seen) < 2 else time.monotonic() - 1

    _, screens, found = b.scan(
        SCROLL, "missing", page("first"), deadline=deadline,
        on_observe=lambda observed: seen.append(observed),
    )
    assert screens == 2 and not found
    assert len(seen) == 2


def test_scan_walks_a_long_list_to_the_bottom():
    """A productive scan is allowed to keep going - it is only stopped by yield or a bound."""
    pages = [page(*(f"item {i}" for i in range(i + 1))) for i in range(1, 20)]
    pages[-1] = page(*["item %d" % i for i in range(20)], "Dude Perfect S9E9")
    b = stub(pages)
    out, screens, found = b.scan(SCROLL, "Dude Perfect", page("item 0"))
    assert found is True
    assert screens == len(pages)
