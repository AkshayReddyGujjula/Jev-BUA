"""Structured assertions read off the real page, plus the facts they need.

These checks are the verdict for a run: the agent drives, the assertions decide. They are
deterministic code, never a model, which is what makes them independent of the loop's own
DONE choice. Moved here from scripts/run_checks.py so the library, the CLI and the MCP
server all judge a run the same way.
"""

import json
import re

# Every key an expectation may carry. Anything else is a caller mistake, and a mistake must
# never read as a pass: an unknown key would add no check, and "all of zero checks passed"
# is True.
TEXT_LIST_KEYS = ("text_contains", "text_contains_all", "text_absent")
STRING_KEYS = ("url_contains", "url_matches", "title_contains")
EXPECT_KEYS = frozenset(
    TEXT_LIST_KEYS + STRING_KEYS
    + ("selector_count", "value_contains", "harvest_at_least", "no_page_errors")
)

# Installed before any page script on every document the run's tab loads, so uncaught
# exceptions, unhandled rejections and console.error calls are visible to the readback.
# It only records; it never changes how the page behaves.
ERROR_RECORDER = (
    "(() => { if (window.__jevErrors) return; const out = window.__jevErrors = [];"
    "const keep = m => { if (out.length < 50) out.push(String(m).slice(0, 300)); };"
    "addEventListener('error', e => keep(e.message || (e.target && e.target.src"
    " ? 'failed to load ' + e.target.src : 'error')), true);"
    "addEventListener('unhandledrejection', e => keep('unhandled rejection: '"
    " + ((e.reason && e.reason.message) || e.reason)));"
    "const original = console.error; console.error = function (...args) {"
    " keep(args.map(a => (a && a.message) || a).join(' ')); return original.apply(this, args); };"
    "})()"
)


def normalize_expect(expect):
    """Validate a caller's expectation and return it in canonical shape, or raise ValueError.

    `text_contains` / `text_absent` take a list of phrases ("any of" / "none of"). A bare
    string is one phrase, not a sequence of characters: iterating it would let a single
    matching letter verify a page.
    """
    if expect is None or expect == {}:
        return None
    if not isinstance(expect, dict):
        raise ValueError("expect must be an object of assertion keys")
    unknown = sorted(set(expect) - EXPECT_KEYS)
    if unknown:
        raise ValueError(f"unknown expect key(s) {unknown}; allowed: {sorted(EXPECT_KEYS)}")
    out = {}
    for key in STRING_KEYS:
        if key in expect:
            value = expect[key]
            if not isinstance(value, str) or not value:
                raise ValueError(f"expect.{key} must be a non-empty string")
            if key == "url_matches":
                try:
                    re.compile(value)
                except re.error as error:
                    raise ValueError(f"expect.url_matches is not a valid regex: {error}") from error
            out[key] = value
    for key in TEXT_LIST_KEYS:
        if key in expect:
            value = expect[key]
            if isinstance(value, str):
                value = [value]
            if (
                not isinstance(value, (list, tuple))
                or not value
                or not all(isinstance(v, str) and v for v in value)
            ):
                raise ValueError(f"expect.{key} must be a non-empty string or list of strings")
            out[key] = list(value)
    for key in ("selector_count", "value_contains"):
        if key in expect:
            rule = expect[key]
            if not isinstance(rule, dict) or not isinstance(rule.get("selector"), str) \
                    or not rule["selector"]:
                raise ValueError(f"expect.{key} must be an object with a non-empty 'selector'")
            if key == "selector_count":
                at_least = rule.get("at_least", 1)
                # 0 would pass on any page: a vacuous check is a mistake, not a verdict.
                if isinstance(at_least, bool) or not isinstance(at_least, int) or at_least < 1:
                    raise ValueError("expect.selector_count.at_least must be an integer >= 1")
                out[key] = {"selector": rule["selector"], "at_least": at_least}
            else:
                # An empty string is contained in every value, so it could never fail.
                if not isinstance(rule.get("text"), str) or not rule["text"]:
                    raise ValueError("expect.value_contains.text must be a non-empty string")
                out[key] = {"selector": rule["selector"], "text": rule["text"]}
    if "harvest_at_least" in expect:
        value = expect["harvest_at_least"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expect.harvest_at_least must be an integer >= 1")
        out["harvest_at_least"] = value
    if "no_page_errors" in expect:
        # Only `true` asks for anything; `false` would be a check that can never fail.
        if expect["no_page_errors"] is not True:
            raise ValueError("expect.no_page_errors must be true")
        out["no_page_errors"] = True
    return out


def evaluate_expectations(expect, page_state):
    """Judge a finished run against the real page. Returns (verified, per-check detail).

    `verified` is None when nothing was asked for, which callers must not read as success:
    no expectation means no verification. `page_state` needs url, title, text and
    selector_counts.
    """
    expect = normalize_expect(expect)
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
    if "text_contains_all" in expect:
        wanted = expect["text_contains_all"]
        missing = [w for w in wanted if w.lower() not in lower]
        record("text_contains_all", not missing, f"missing {missing} of {wanted}")
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
    if "no_page_errors" in expect:
        errors = page_state.get("errors")
        if errors is None:
            # The recorder was not installed on this document; absence of evidence is no pass.
            record("no_page_errors", False, "page error recorder unavailable")
        else:
            record("no_page_errors", not errors, f"{len(errors)} error(s): {errors[:3]}")
    if not detail:
        # Unreachable after normalize_expect, and kept so zero checks can never verify.
        return None, []
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
    """Read the current page through a live Browser, or fail the whole readback.

    Reading again here, through the browser itself, is what makes the result independent
    of whatever the agent last observed.
    """
    # One JavaScript evaluation sees one document state. Independent CDP reads
    # could mix text from one route with a URL or form value from the next.
    expression = (
        "(() => {"
        f"const counts = Object.fromEntries({json.dumps(list(selectors))}.map(s => "
        "[s, document.querySelectorAll(s).length]));"
        f"const values = Object.fromEntries({json.dumps(list(value_selectors))}.map(s => "
        "[s, document.querySelector(s)?.value ?? '']));"
        "return {url:location.href,title:document.title,text:document.body.innerText,"
        "selector_counts:counts,values:values,"
        "errors:Array.isArray(window.__jevErrors)?window.__jevErrors.slice():null};"
        "})()"
    )
    facts = browser.evaluate(expression)
    if not isinstance(facts, dict) or not facts.get("url"):
        raise RuntimeError("Live readback did not return complete page facts")
    return facts
