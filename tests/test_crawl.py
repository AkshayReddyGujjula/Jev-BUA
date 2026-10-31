"""`jev crawl` verdicts are plain readings of the page, never a model's opinion."""

from AgentJevUltraFast.crawl import verdict


def test_page_verdicts():
    assert verdict("", []) == ("blank", "the page rendered no text")
    assert verdict("Home\nWelcome", ["TypeError: x is undefined"])[0] == "js-errors"
    assert verdict("404\nPage not found", [])[0] == "error-page"
    assert verdict("Something went wrong. Try again.", [])[0] == "error-page"
    assert verdict("Pricing\nPlans for teams", []) == ("ok", "")
    # a word inside another word is not an error page
    assert verdict("Notfoundry design studio", [])[0] == "ok"
