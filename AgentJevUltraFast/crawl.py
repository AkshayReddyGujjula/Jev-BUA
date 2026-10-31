"""`jev crawl`: find broken pages in a running app with zero model calls.

Starting from one URL, every same-origin page the app links to is opened by plain navigation (a GET,
never a click, never a link that looks like it signs out or deletes), breadth first, up to a page
budget. Each page is read once for what a user would notice first: JavaScript errors (the same
recorder `no page errors` checks use), a page that renders nothing, or a page that says it is an
error. Nothing is decided by a model, so the verdicts are as plain as the evidence behind them.
"""

import json
import os
import re
import time
from pathlib import Path

from .draft import SAME_ORIGIN_LINKS, linked_paths

MAX_PAGES = 25
ERROR_PAGE = re.compile(
    r"\b(?:page not found|not found|something went wrong|unexpected error|"
    r"application error|internal server error|cannot get /)\b", re.IGNORECASE)
# A bare status code only counts where an error page puts it: the title or the first line.
# "Join 500+ teams" or "Order #404" further down is ordinary copy.
STATUS_CODE = re.compile(r"^\W*(?:404|500|502|503)\b")
ERRORS = "window.__jevErrors ? window.__jevErrors.slice(0, 10) : null"
TEXT = "document.body ? document.body.innerText : ''"


def same_origin(origin):
    """scheme://host[:port] in lower case, without the default port (location.origin omits it)."""
    from urllib.parse import urlsplit

    parts = urlsplit((origin or "").lower())
    port = parts.port
    if port is None or (parts.scheme, port) in {("http", 80), ("https", 443)}:
        return f"{parts.scheme}://{parts.hostname or ''}"
    return f"{parts.scheme}://{parts.hostname}:{port}"


def verdict(text, errors, title=""):
    """(status, note) for one page: the first problem a user would notice, or ok."""
    head = " ".join((text or "").split())[:400]
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    if not head:
        return "blank", "the page rendered no text"
    if errors:
        return "js-errors", f"{len(errors)} error(s): {errors[0][:160]}"
    match = ERROR_PAGE.search(head) or STATUS_CODE.search(title or "") or STATUS_CODE.search(first)
    if match:
        return "error-page", f"says {match.group(0).strip()!r}: {head[:120]}"
    return "ok", ""


def crawl(url, max_pages=MAX_PAGES):
    """Visit the app breadth first from `url`; return one record per page visited."""
    from urllib.parse import urlsplit

    from . import Browser

    parts = urlsplit(url)
    origin = same_origin(f"{parts.scheme}://{parts.netloc}")
    start = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    queue, seen, pages = [(start, None)], {start}, []
    browser = Browser(url)
    try:
        while queue and len(pages) < max_pages:
            path, found_on = queue.pop(0)
            started = time.perf_counter()
            record = {"path": path, "found_on": found_on}
            try:
                if pages:                                   # the first page is already open
                    browser.navigate(origin + path)
                landed = browser.evaluate("location.origin") or ""
                if same_origin(landed) != origin:
                    # A redirect to a sign-in provider or another site: not this app's page, not
                    # its problem, and its links are not this app's links.
                    record.update(status="left-origin", note=f"redirected to {landed}"[:200], errors=[])
                    if not pages:
                        # The start page itself went elsewhere: nothing of this app was checked,
                        # which must never read as a clean pass.
                        record.update(status="start-redirected",
                                      note=f"the start page redirected to {landed}; crawl that URL instead"[:200])
                    links = []
                else:
                    text = browser.evaluate(TEXT) or ""
                    errors = browser.evaluate(ERRORS) or []
                    record["title"] = browser.evaluate("document.title") or ""
                    record["status"], record["note"] = verdict(text, errors, record["title"])
                    record["errors"] = errors
                    links = browser.evaluate(SAME_ORIGIN_LINKS) or []
            except Exception as error:
                record["status"], record["note"], links = "unreachable", f"{type(error).__name__}: {error}"[:200], []
            record["load_s"] = round(time.perf_counter() - started, 2)
            pages.append(record)
            for link in linked_paths(links, limit=10_000):
                if link not in seen:
                    seen.add(link)
                    queue.append((link, path))
    finally:
        browser.close()
    return {"start": url, "pages": pages, "unvisited": [p for p, _ in queue],
            "problems": sum(1 for p in pages if p["status"] not in {"ok", "left-origin"})}


def main(argv):
    import argparse

    from .checks import load_env

    parser = argparse.ArgumentParser(prog="jev crawl", description=__doc__)
    parser.add_argument("url", help="the running app's start page, e.g. http://localhost:3000")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--out", default="", help="report path (default jev-tests/.jev/reports/crawl.json)")
    args = parser.parse_args(argv)
    load_env()
    os.environ.setdefault("JEV_CHROME", "dedicated")
    # An alert() on load would freeze the page and every later read; a crawl answers it.
    os.environ.setdefault("JEV_DIALOGS", "dismiss")
    report = crawl(args.url, max(1, args.max_pages))
    for page in report["pages"]:
        mark = "ok  " if page["status"] in {"ok", "left-origin"} else "FAIL"
        print(f"{mark} {page['path'][:60]:<60} {page['status']:<12} {page.get('note', '')[:70]}")
    if report["unvisited"]:
        print(f"... {len(report['unvisited'])} more linked pages not visited (--max-pages)")
    target = Path(args.out or Path("jev-tests") / ".jev" / "reports" / "crawl.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{len(report['pages'])} pages, {report['problems']} with problems, 0 model calls; report: {target}")
    return 1 if report["problems"] else 0
