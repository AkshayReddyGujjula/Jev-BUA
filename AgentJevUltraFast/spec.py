"""Markdown test specs: plain-English tests a person or a coding agent can write and review.

    # StudyCanvas smoke                      <- optional title
    base_url: http://localhost:5173          <- suite settings (key: value)
    isolate: storage

    ## Unknown URL shows the 404 page        <- one test per "##" heading
    path: /workspace/no-such-place           <- test settings
    values:
      - Access code: local-test              <- field label: literal to type

    1. Read the page.                        <- numbered steps, each one goal for Jev
       - see: Page not found                 <- checks read off the real page after the step
       - not see: Something went wrong
    2. Click Go home.
       - url contains: /workspace

A step bullet `- open: /path` is not a check: it loads that path in the same tab before the step.

A test headed `## Before all` runs once per suite run, before every test, and the tests start
from the state it leaves (signed in, onboarded): sign in once instead of once per test.

A test headed `## Before each` is not a test: its steps (and values, and path when a test has
none) are put in front of every test, e.g. signing in.

Checks (all must hold): `see: X` (text present), `see any: A | B`, `not see: X`,
`url contains: X`, `url matches: REGEX`, `title contains: X`, `count: SELECTOR >= N`,
`value: SELECTOR contains X`, `no page errors`, `not blank`.

The steps are goals, never scripts: Jev decides how to do each one on the live page, and only
the checks decide pass or fail. A spec compiles to the same check file `jev test` runs from JSON,
so nothing here can weaken a verdict: a malformed check rejects the whole file.
"""

import re

SUITE_KEYS = {"base_url", "isolate", "commit_floor", "max_seconds", "max_steps", "tab", "review", "viewport", "dialogs",
              "replay_commits", "stop_on_expect", "mode", "check_wait"}
TEST_KEYS = {"path", "isolate", "commit_floor", "max_seconds", "max_steps", "tab", "review", "viewport", "dialogs",
             "replay_commits", "stop_on_expect", "mode", "check_wait"}
BEFORE_EACH = {"before each", "before each test", "setup"}
BEFORE_ALL = {"before all", "before all tests", "global setup"}
STEP = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
SETTING = re.compile(r"^([A-Za-z_][\w ]*?)\s*:\s*(.*?)\s*$")
COUNT = re.compile(r"^(.+?)\s*>=\s*(\d+)$")
VALUE = re.compile(r"^(.+?)\s+contains\s+(.+)$", re.IGNORECASE)


class SpecError(ValueError):
    pass


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        return text[1:-1]
    return text


def _scalar(key, raw):
    value = _unquote(raw)
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if key in {"commit_floor", "check_wait"}:
        return float(value.removesuffix("s").strip())
    if key in {"max_seconds", "max_steps"}:
        return int(value)
    return value


def _check(expect, text, where):
    """Add one check line to an expect dict, or raise SpecError naming the line."""
    body = text.strip()
    lowered = body.lower()
    if lowered in {"no page errors", "no errors", "no console errors"}:
        expect["no_page_errors"] = True
        return
    if lowered in {"not blank", "page not blank", "page has content"}:
        expect["not_blank"] = True
        return
    key, sep, value = body.partition(":")
    if not sep or not value.strip():
        raise SpecError(f"{where}: cannot read check {body!r}")
    key, value = key.strip().lower(), value.strip()
    single = {"url contains": "url_contains", "url matches": "url_matches", "title contains": "title_contains",
              "count": "selector_count", "value": "value_contains"}
    if single.get(key) in expect:
        # One of these per step: a second line would silently replace the first check.
        raise SpecError(f"{where}: only one '{key}:' check per step; split the step instead")
    if key in {"see", "sees", "shows", "text"}:
        expect.setdefault("text_contains_all", []).append(_unquote(value))
    elif key in {"see any", "see any of", "shows any"}:
        expect.setdefault("text_contains", []).extend(
            _unquote(part) for part in value.split("|") if part.strip())
    elif key in {"not see", "don't see", "do not see", "never see", "hidden", "no text"}:
        expect.setdefault("text_absent", []).append(_unquote(value))
    elif key == "url contains":
        expect["url_contains"] = _unquote(value)
    elif key == "url matches":
        expect["url_matches"] = _unquote(value)
    elif key == "title contains":
        expect["title_contains"] = _unquote(value)
    elif key == "count":
        match = COUNT.match(value)
        if not match:
            raise SpecError(f"{where}: count needs 'SELECTOR >= N', got {value!r}")
        expect["selector_count"] = {"selector": _unquote(match[1]), "at_least": int(match[2])}
    elif key == "value":
        match = VALUE.match(value)
        if not match:
            raise SpecError(f"{where}: value needs 'SELECTOR contains TEXT', got {value!r}")
        expect["value_contains"] = {"selector": _unquote(match[1]), "text": _unquote(match[2])}
    else:
        raise SpecError(f"{where}: unknown check {key!r}")


def parse(text, name="spec"):
    """Compile Markdown spec text into a check-file dict (base_url, defaults, checks)."""
    suite = {"defaults": {}, "checks": []}
    test = None
    step = None
    values_target = None
    for number, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{number}"
        stripped = line.strip()
        if not stripped or stripped.startswith(("<!--", ">")):
            continue
        if stripped.startswith("## "):
            test = {"name": stripped[3:].strip(), "steps": []}
            suite["checks"].append(test)
            step, values_target = None, None
            continue
        if stripped.startswith("# "):
            suite.setdefault("title", stripped[2:].strip())
            continue
        found = STEP.match(line)
        if found and test is not None:
            step = {"goal": found[2]}
            test["steps"].append(step)
            values_target = None
            continue
        bullet = BULLET.match(line)
        if bullet and values_target is not None:
            label, sep, value = bullet[1].partition(":")
            if not sep or not label.strip():
                raise SpecError(f"{where}: a value line is 'Field label: text', got {bullet[1]!r}")
            values_target[_unquote(label)] = _unquote(value)
            continue
        if bullet and step is None and test is not None:
            # A check before step 1 belongs to no step; dropping it would hide it.
            raise SpecError(f"{where}: a check bullet must sit under a numbered step")
        if bullet and step is not None:
            key, sep, value = bullet[1].partition(":")
            if sep and key.strip().lower() == "open" and value.strip():
                # Not a check: load this path in the same tab before the step runs.
                step["path"] = _unquote(value)
                continue
            _check(step.setdefault("expect", {}), bullet[1], where)
            continue
        setting = SETTING.match(stripped)
        if setting and step is None:
            key = setting[1].strip().lower().replace(" ", "_")
            raw = setting[2]
            scope = test if test is not None else suite
            if key == "values":
                values_target = scope.setdefault("values", {})
                if raw:
                    raise SpecError(f"{where}: list values on their own '- Label: text' lines")
                continue
            allowed = TEST_KEYS if test is not None else SUITE_KEYS
            if key not in allowed and " " in setting[1].strip() and key not in SUITE_KEYS | TEST_KEYS:
                continue                      # "The fourth app: ..." is prose, not a setting
            if key not in allowed:
                raise SpecError(f"{where}: unknown setting {key!r}")
            try:
                value = _scalar(key, raw)
            except ValueError as error:
                raise SpecError(f"{where}: bad value for {key}: {raw!r}") from error
            if test is None and key != "base_url":
                suite["defaults"][key] = value
            elif test is None:
                suite["base_url"] = value
            else:
                test[key] = value
            continue
        if step is None:
            continue                          # free prose under a heading documents the test
        raise SpecError(f"{where}: expected a check bullet under step {step['goal']!r}")
    once = [test for test in suite["checks"] if test["name"].strip().lower() in BEFORE_ALL]
    setup = [test for test in suite["checks"] if test["name"].strip().lower() in BEFORE_EACH]
    suite["checks"] = [test for test in suite["checks"] if test not in setup and test not in once]
    if len(once) > 1:
        raise SpecError(f"{name}: only one '## Before all' block is allowed")
    if once:
        if not once[0]["steps"]:
            raise SpecError(f"{name}: '## Before all' has no numbered steps")
        if not any(step.get("expect") for step in once[0]["steps"]):
            # Every test waits on it, so a setup that cannot verify would block the whole suite.
            raise SpecError(f"{name}: '## Before all' needs a check (e.g. '- see: Dashboard') "
                            "so the run can tell that setup worked")
        # Runs once, before every test, and the tests start from the state it leaves.
        suite["before_all"] = once[0]
    if not suite["checks"]:
        raise SpecError(f"{name}: no tests (each test starts with a '## ' heading)")
    for test in setup + suite["checks"]:
        if not test["steps"]:
            raise SpecError(f"{name}: test {test['name']!r} has no numbered steps")
    if len(setup) > 1:
        raise SpecError(f"{name}: only one '## Before each' block is allowed")
    if setup:
        # Shared setup (sign in, finish onboarding) runs at the start of every test, in that
        # test's own tab, so tests stay independent. Its values apply under the test's own.
        before = setup[0]
        for test in suite["checks"]:
            own = test["steps"]
            if before.get("path") and test.get("path"):
                # Setup runs where it was written for; the test's own page loads after it (even
                # the same address: a fresh load is how a test leaves wherever setup ended).
                own = [{**own[0], "path": test["path"]}] + own[1:]
            if before.get("path"):
                test["path"] = before["path"]
            test["steps"] = [dict(step) for step in before["steps"]] + own
            if before.get("values"):
                test["values"] = {**before["values"], **test.get("values", {})}
    if "values" in suite:
        suite["defaults"]["values"] = suite.pop("values")
    return suite


def load(path):
    from pathlib import Path

    path = Path(path)
    return parse(path.read_text(encoding="utf-8-sig"), name=path.name)


def annotate(text, suggestions):
    """The spec with each step's suggested checks added under it as comments, for a human to adopt.

    `suggestions` maps a test name to {step number as run: ["see: X", ...]}; with `## Before each`
    the run's step numbers include the setup steps, so they are counted out here. Comments are
    ignored by the parser: nothing suggested ever becomes a check until someone uncomments it.
    """
    setup_steps = 0
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
        elif section in BEFORE_EACH and STEP.match(line):
            setup_steps += 1
    out, test, own = [], None, 0
    for line in text.splitlines():
        out.append(line)
        stripped = line.strip()
        if stripped.startswith("## "):
            name = stripped[3:].strip()
            test = None if name.lower() in BEFORE_EACH | BEFORE_ALL else name
            own = 0
            continue
        if test is not None and STEP.match(line):
            own += 1
            for suggestion in (suggestions.get(test) or {}).get(own + setup_steps, []):
                out.append(f"   <!-- suggested: - {suggestion} -->")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
