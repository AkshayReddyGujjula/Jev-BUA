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


def test_numbers_in_ordinary_copy_are_not_error_pages():
    assert verdict("Pricing\nJoin 500+ teams. Order #404 shipped.", [])[0] == "ok"
    assert verdict("404\nThis page does not exist", [])[0] == "error-page"
    assert verdict("Oops\nTry again", [], title="500 Internal error")[0] == "error-page"


def test_risky_links_are_never_followed():
    from AgentJevUltraFast.draft import linked_paths
    paths = ["/users/sign_out", "/log_out", "/logoff", "/account/deactivate", "/exit", "/docs", "/exits-and-entries"]
    assert linked_paths(paths, limit=10) == ["/docs", "/exits-and-entries"]
