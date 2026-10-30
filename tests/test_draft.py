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
    assert "<!-- predicted: not on the start page -->\n   - see: Results for" in markdown
    assert "<!-- weak: already on the start page -->\n   - see: Shop" in markdown


def test_page_brief_keeps_labelled_controls_only():
    page = {"url": "u", "title": "t", "text": "x" * 5000, "actions": [
        {"kind": "click", "role": "button", "label": "Save"},
        {"kind": "scroll", "label": "Scroll down"},
        {"kind": "fill", "role": "textbox", "label": ""},
    ]}
    brief = draft.page_brief(page)
    assert brief["controls"] == ["click button: Save"] and len(brief["text"]) == 4000
