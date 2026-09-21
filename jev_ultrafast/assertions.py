"""Structured assertions read off the real page, plus the facts they need.

These checks are the verdict for a run: the agent drives, the assertions decide. They are
deterministic code, never a model, which is what makes them independent of the loop's own
DONE choice. Moved here from scripts/run_checks.py so the library, the CLI and the MCP
server all judge a run the same way.
"""

import json
import re


def evaluate_expectations(expect, page_state):
    """Judge a finished run against the real page. Returns (verified, per-check detail).

    `verified` is None when nothing was asked for, which callers must not read as success:
    no expectation means no verification. `page_state` needs url, title, text and
    selector_counts.
    """
    if not expect:
        return None, []
    detail = []
    text = (page_state.get("text") or "")
    lower = text.lower()
    url = page_state.get("url") or ""
    title = page_state.get("title") or ""

    def record(name, ok, note):
        detail.append({"check": name, "pass": bool(ok), "note": note})

    if "url_contains" in expect:
        wanted = expect["url_contains"]
        record("url_contains", wanted in url, f"{wanted!r} in {url!r}")
    if "url_matches" in expect:
        pattern = expect["url_matches"]
        record("url_matches", re.search(pattern, url) is not None, f"/{pattern}/ vs {url!r}")
    if "title_contains" in expect:
        wanted = expect["title_contains"]
        record("title_contains", wanted.lower() in title.lower(), f"{wanted!r} in {title!r}")
    if "text_contains" in expect:
        wanted = expect["text_contains"]
        hits = [w for w in wanted if w.lower() in lower]
        record("text_contains", bool(hits), f"matched {hits} of {wanted}")
    if "text_absent" in expect:
        unwanted = expect["text_absent"]
        hits = [w for w in unwanted if w.lower() in lower]
        record("text_absent", not hits, f"unexpectedly present: {hits}")
    if "selector_count" in expect:
        rule = expect["selector_count"]
        count = page_state.get("selector_counts", {}).get(rule["selector"], 0)
        record("selector_count", count >= rule.get("at_least", 1),
               f"{rule['selector']} -> {count} (need >= {rule.get('at_least', 1)})")
    if "value_contains" in expect:
        rule = expect["value_contains"]
        value = (page_state.get("values") or {}).get(rule.get("selector"), "")
        wanted = str(rule.get("text", ""))
        record("value_contains", wanted.lower() in (value or "").lower(),
               f"{rule.get('selector')} -> {value!r} (need {wanted!r})")
    if "harvest_at_least" in expect:
        # Collector artifact check: this run's stored items, each already matched against
        # page text the run observed. It is not a live-page readback, so pair it with a page
        # check when the verdict must stand on the page alone.
        count = int((page_state.get("harvest") or {}).get("items") or 0)
        record("harvest_at_least", count >= int(expect["harvest_at_least"]),
               f"harvest -> {count} (need >= {expect['harvest_at_least']})")
    return all(d["pass"] for d in detail), detail


def selectors_for(expect):
    """The selectors a selector_count expectation needs counted on the page."""
    if expect and "selector_count" in expect:
        return [expect["selector_count"]["selector"]]
    return []


def value_selectors_for(expect):
    """The selectors a value_contains expectation needs read on the page."""
    rule = (expect or {}).get("value_contains")
    if isinstance(rule, dict) and rule.get("selector"):
        return [str(rule["selector"])]
    return []


def page_facts(browser, selectors=(), value_selectors=()):
    """Read the current page through a live Browser. Missing values degrade, never raise.

    Reading again here, through the browser itself, is what makes the result independent
    of whatever the agent last observed.
    """
    counts = {}
    for selector in selectors:
        try:
            counts[selector] = browser.evaluate(
                f"document.querySelectorAll({json.dumps(selector)}).length"
            ) or 0
        except Exception:
            counts[selector] = 0
    values = {}
    for selector in value_selectors:
        try:
            values[selector] = browser.evaluate(
                f"(document.querySelector({json.dumps(selector)}) || {{}}).value ?? ''"
            ) or ""
        except Exception:
            values[selector] = ""
    text = ""
    try:
        text = browser.evaluate("document.body.innerText") or ""
    except Exception:
        pass
    try:
        url = browser.evaluate("location.href") or getattr(browser, "last_url", "")
    except Exception:
        url = getattr(browser, "last_url", "")
    try:
        title = browser.evaluate("document.title") or ""
    except Exception:
        title = ""
    return {"url": url, "title": title, "text": text, "selector_counts": counts, "values": values}
