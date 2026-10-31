import pytest

from AgentJevUltraFast import spec
from AgentJevUltraFast.assertions import normalize_expect

SAMPLE = """\
# Shop smoke
base_url: http://localhost:3000
isolate: storage
commit_floor: 0.6
values:
  - Email: ada@example.com

Free prose about the suite is ignored.

## Search shows results
path: /search
values:
  - Search: laptops

1. Search for laptops.
   - see: Results for
   - see any: laptops | notebooks
   - not see: No results
   - url contains: q=laptops
2. Open the first result.
   - count: .price >= 1
   - value: #qty contains 1
   - no page errors

## Unknown page
path: /nope
1. Read the page.
   - see: "Page not found"
"""


def test_markdown_compiles_to_the_check_file_shape():
    suite = spec.parse(SAMPLE)
    assert suite["base_url"] == "http://localhost:3000"
    assert suite["defaults"] == {"isolate": "storage", "commit_floor": 0.6,
                                 "values": {"Email": "ada@example.com"}}
    first, second = suite["checks"]
    assert first["name"] == "Search shows results" and first["path"] == "/search"
    assert first["values"] == {"Search": "laptops"}
    assert [s["goal"] for s in first["steps"]] == ["Search for laptops.", "Open the first result."]
    assert first["steps"][0]["expect"] == {
        "text_contains_all": ["Results for"], "text_contains": ["laptops", "notebooks"],
        "text_absent": ["No results"], "url_contains": "q=laptops"}
    assert first["steps"][1]["expect"] == {
        "selector_count": {"selector": ".price", "at_least": 1},
        "value_contains": {"selector": "#qty", "text": "1"}, "no_page_errors": True}
    assert second["steps"][0]["expect"] == {"text_contains_all": ["Page not found"]}
    for test in suite["checks"]:
        for step in test["steps"]:
            assert normalize_expect(step["expect"])     # every compiled check is valid


@pytest.mark.parametrize("bad, message", [
    ("## T\n1. Go.\n   - smell: roses\n", "unknown check"),
    ("## T\n1. Go.\n   - count: .x\n", "SELECTOR >= N"),
    ("## T\nwarp: 9\n1. Go.\n", "unknown setting"),
    ("## T\nJust prose, no steps.\n", "no numbered steps"),
    ("# Only a title\n", "no tests"),
])
def test_a_malformed_spec_is_rejected_with_its_line(bad, message):
    with pytest.raises(spec.SpecError, match=message):
        spec.parse(bad)


def test_a_step_without_checks_is_allowed_and_has_no_expectation():
    suite = spec.parse("## T\n1. Open the menu.\n2. Pick Settings.\n   - see: Settings\n")
    steps = suite["checks"][0]["steps"]
    assert "expect" not in steps[0] and steps[1]["expect"] == {"text_contains_all": ["Settings"]}


def test_before_each_steps_run_first_in_every_test():
    suite = spec.parse("""\
## Before each
path: /login
values:
  - Email: ada@example.com
1. Sign in.
   - see: Dashboard

## Create a project
values:
  - Name: Apollo
1. Create a project.
   - see: Apollo

## Settings
path: /settings
1. Open settings.
""")
    create, settings = suite["checks"]
    assert [s["goal"] for s in create["steps"]] == ["Sign in.", "Create a project."]
    assert create["values"] == {"Email": "ada@example.com", "Name": "Apollo"}
    assert create["path"] == "/login" and "path" not in create["steps"][1]
    # Settings signs in on /login first, then opens its own page for its own first step.
    assert settings["path"] == "/login" and settings["steps"][1]["path"] == "/settings"
    assert len(suite["checks"]) == 2


def test_open_bullet_loads_a_path_before_the_step():
    suite = spec.parse("## T\npath: /a\n1. Edit.\n   - see: Unsaved\n2. Read.\n   - open: /b\n   - see: Items\n")
    second = suite["checks"][0]["steps"][1]
    assert second["path"] == "/b" and second["expect"] == {"text_contains_all": ["Items"]}


@pytest.mark.parametrize("bad, message", [
    ("## T\n1. Go.\n   - count: .a >= 1\n   - count: .b >= 1\n", "only one 'count:'"),
    ("## T\n   - see: X\n1. Go.\n", "must sit under a numbered step"),
    ("## Before each\n1. A.\n## Setup\n1. B.\n## T\n1. C.\n", "only one '## Before each'"),
])
def test_checks_are_never_dropped_silently(bad, message):
    with pytest.raises(spec.SpecError, match=message):
        spec.parse(bad)


def test_before_all_runs_once_and_is_not_a_test():
    suite = spec.parse("## Before all\npath: /login\n1. Sign in.\n   - see: Home\n\n## A\n1. Go.\n")
    assert [t["name"] for t in suite["checks"]] == ["A"]
    assert suite["before_all"]["steps"][0]["goal"] == "Sign in." and suite["before_all"]["path"] == "/login"
    assert suite["checks"][0]["steps"] == [{"goal": "Go."}]       # not prepended to tests



def test_check_wait_is_seconds_and_becomes_a_suite_default():
    suite = spec.parse("check_wait: 20s\n\n## Slow page\n1. Go.\n   - see: Ready\n")
    assert suite["defaults"]["check_wait"] == 20.0


def test_before_all_without_a_check_is_refused_at_parse_time():
    import pytest
    with pytest.raises(spec.SpecError, match="needs a check"):
        spec.parse("## Before all\n1. Sign in.\n\n## T\n1. Go.\n   - see: X\n")


def test_suggestions_are_written_as_comments_under_their_step():
    text = ("## Before each\npath: /login\n1. Sign in.\n   - see: Home\n\n"
            "## Create a space\n1. Open Spaces.\n2. Create one named Alpha.\n   - see: Alpha\n")
    # As run, the test's steps are [Sign in, Open Spaces, Create...]: step 2 is "Open Spaces".
    out = spec.annotate(text, {"Create a space": {2: ["see: New space", "not see: No spaces yet"]}})
    assert ("1. Open Spaces.\n   <!-- suggested: - see: New space -->\n"
            "   <!-- suggested: - not see: No spaces yet -->\n2.") in out
    assert spec.parse(out)["checks"] == spec.parse(text)["checks"]       # comments never become checks


def test_not_blank_is_a_check_and_a_blank_page_fails_it():
    from AgentJevUltraFast.assertions import evaluate_expectations
    expect = spec.parse("## T\n1. Read the page.\n   - not blank\n")["checks"][0]["steps"][0]["expect"]
    assert expect == {"not_blank": True}
    assert evaluate_expectations(expect, {"text": "   "})[0] is False
    assert evaluate_expectations(expect, {"text": "Welcome"})[0] is True
