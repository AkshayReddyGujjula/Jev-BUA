from AgentJevUltraFast import draft, spec


def test_drafted_plan_becomes_a_spec_that_parses_and_marks_its_weak_checks():
    plan = {"title": "Shop", "tests": [
        {"name": "Search works", "steps": [
            {"goal": 'Search for "laptops"', "see": ["Results for", "Shop"], "not_see": ["No results"]}]},
        {"name": "Empty", "steps": []},
        {"name": "Bad step", "steps": ["not a dict"]},
    ]}
    markdown = draft.to_markdown(plan, "http://localhost:3000", "/", "Welcome to Shop\nSearch")
    suite = spec.parse(markdown)
    assert [t["name"] for t in suite["checks"]] == ["Search works"]
    step = suite["checks"][0]["steps"][0]
    assert step["goal"] == 'Search for "laptops"'
    assert step["expect"] == {"text_contains_all": ["Results for", "Shop"], "text_absent": ["No results"]}
    assert "<!-- predicted: not on any page the drafter read -->\n   - see: Results for" in markdown
    assert "<!-- weak: already on the start page -->\n   - see: Shop" in markdown


def test_page_brief_keeps_labelled_controls_only():
    page = {"url": "u", "title": "t", "text": "x" * 5000, "actions": [
        {"kind": "click", "role": "button", "label": "Save"},
        {"kind": "scroll", "label": "Scroll down"},
        {"kind": "fill", "role": "textbox", "label": ""},
    ]}
    brief = draft.page_brief(page)
    assert brief["controls"] == ["click button: Save"] and len(brief["text"]) == 4000


def test_linked_pages_are_same_origin_paths_without_risky_words():
    from AgentJevUltraFast import draft
    paths = ["/how-it-works", "/play", "/logout", "/account/delete", "/play", "https://x.test/a", "/faq", "/a", "/b"]
    assert draft.linked_paths(paths, limit=4) == ["/how-it-works", "/play", "/faq", "/a"]


def test_a_check_seen_on_a_linked_page_is_not_predicted():
    from AgentJevUltraFast import draft
    plan = {"title": "T", "tests": [{"name": "Open the game", "steps": [
        {"goal": "Click Play", "see": ["WELCOME, NEWCOMER!", "Never shown"]}]}]}
    markdown = draft.to_markdown(plan, "http://localhost:5190", "/", "Moneywell Town\nPlay",
                                 "WELCOME, NEWCOMER!\nGET MY PASS")
    welcome = markdown.index("- see: WELCOME, NEWCOMER!")
    assert "predicted" not in markdown[:welcome].splitlines()[-1]
    assert "<!-- predicted: not on any page the drafter read -->\n   - see: Never shown" in markdown
