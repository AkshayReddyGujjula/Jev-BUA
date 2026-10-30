"""Keys named in the goal become key actions; ordinary words never do."""

from AgentJevUltraFast.keys import combo, goal_keys, with_goal_keys


def labels(goal):
    return [(k["key"], k["modifiers"]) for k in goal_keys(goal)]


def test_quoted_and_combo_keys_from_the_goal():
    assert labels('On Home, press "?" to open the sheet, then try Ctrl+F and Esc.') == [("f", 2), ("?", 0)]
    assert labels("Search, then use ArrowDown twice and press Enter.") == [("Enter", 0), ("ArrowDown", 0)]
    assert labels("Press Cmd+Shift+K") == [("K", 12)]
    # Sentence punctuation is not part of the key (measured: "with Ctrl+K." offered nothing).
    assert labels("Open the command bar with Ctrl+K.") == [("k", 2)]
    assert labels("Then press Tab.") == [("Tab", 0)]


def test_ordinary_words_are_not_keys():
    for goal in ("On Home, click New canvas.", "End the tutorial.", "Delete the canvas named X.",
                 "Open it in a new tab and press the button.", "Enter the access code."):
        assert goal_keys(goal) == [], goal
    assert labels("Move focus with the Tab key.") == [("Tab", 0)]


def test_a_printable_key_is_aimed_at_the_page_and_combos_are_not():
    keys = {k["key"]: k for k in goal_keys('Press "/" then Ctrl+K')}
    assert keys["/"]["shortcut"] is True and keys["k"]["shortcut"] is False
    assert combo("Ctrl+Nope") is None and combo("Hyper+K") is None


def test_keys_the_page_already_offers_are_not_duplicated():
    page = {"actions": [{"id": "key_escape", "kind": "key", "key": "Escape", "modifiers": 0}]}
    merged = with_goal_keys(page, goal_keys("Press Escape, then press ?"))
    assert [a["key"] for a in merged["actions"]] == ["Escape", "?"]
    assert with_goal_keys(page, []) is page
