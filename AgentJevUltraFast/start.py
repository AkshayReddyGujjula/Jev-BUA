"""Where a goal starts, without a model in front of Jev.

`jev "open youtube, search mkbhd ..."` must reach Chrome immediately. An orchestrating chat
model used to read the prompt first and pick the URL: measured 2026-09-24 in opencode, that
hop took ~59 s (9,280 reasoning tokens) before Jev's 4 s run began. This module replaces it
with code:

1. an explicit URL or domain in the goal is used as written;
2. otherwise the site phrase after "open / go to / visit ..." is resolved through a
   first-result search redirect (one HTTP GET, no model, no site list);
3. otherwise there is no start URL and the caller must pass one (or use the active tab).
"""

import os
import re
from urllib.parse import quote_plus, unquote, urlparse

import httpx

URL = re.compile(r"https?://[^\s,;\"'<>)]+", re.IGNORECASE)
# name.tld with an alphabetic TLD, so "v4.1" or "e.g" never count as a domain.
DOMAIN = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,12})(/[^\s,;\"'<>)]*)?", re.IGNORECASE)
NOT_TLD = {"e.g", "i.e", "etc"}
VERB = re.compile(r"\b(?:open|go to|goto|visit|navigate to|head to|launch|load)\s+(?:up\s+)?(.+)", re.IGNORECASE)
# Where the site phrase ends: punctuation, or the next clause of the goal.
STOP = re.compile(
    r"[,;.:!?]|\s(?:and|then|to|for|search|type|find|click|look|in|with|where|so|&)\b",
    re.IGNORECASE,
)
FILLER = re.compile(r"^(?:the|my|our|a|an)\s+|\s+(?:website|site|app|page|homepage|home page)$", re.IGNORECASE)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
SEARCH = os.environ.get("JEV_START_SEARCH", "https://duckduckgo.com/?q=%5C{}")


def explicit(goal):
    """A URL or bare domain written in the goal, as an absolute URL, else None."""
    match = URL.search(goal)
    if match:
        return match.group(0).rstrip(".")
    for match in DOMAIN.finditer(goal):
        if match.group(1).lower() in NOT_TLD:
            continue
        return "https://" + match.group(1) + (match.group(2) or "")
    return None


def site_phrase(goal):
    """The short name after an opening verb ("open YouTube, ..." -> "YouTube"), else None."""
    match = VERB.search(goal)
    if not match:
        return None
    phrase = STOP.split(" " + match.group(1), maxsplit=1)[0].strip()
    previous = None
    while phrase != previous:
        previous, phrase = phrase, FILLER.sub("", phrase).strip()
    words = phrase.split()
    return phrase if 0 < len(words) <= 4 else None


def first_result(phrase, timeout=3.0):
    """The first search result's URL for `phrase` (one GET; the redirect page names it)."""
    url = SEARCH.format(quote_plus(phrase))
    try:
        # A full desktop Chrome UA: with a bare one the first result changed ("studycanvas"
        # went to a different site); with this one it was stable across repeats.
        response = httpx.get(url, timeout=timeout, follow_redirects=False, headers={"User-Agent": UA})
        text = response.text
        location = response.headers.get("location") or ""
    except httpx.HTTPError:
        return None
    for candidate in (location, text):
        found = re.search(r"uddg=([^&'\"]+)", candidate)
        if found:
            target = unquote(found.group(1))
            if urlparse(target).scheme in {"http", "https"}:
                return target
    # Some names redirect straight to their target ("gmail") rather than through uddg.
    found = re.search(r"url=(https?://[^'\"]+)", text)
    if found and "duckduckgo.com" not in found.group(1):
        return found.group(1)
    if location.startswith(("http://", "https://")) and "duckduckgo.com" not in location:
        return location
    return None


def resolve(goal):
    """(url, how) for the goal's start page; url is None when nothing names a site."""
    url = explicit(goal)
    if url:
        return url, "explicit"
    phrase = site_phrase(goal)
    if not phrase:
        return None, "none"
    url = first_result(phrase)
    if url:
        return url, f"first result for {phrase!r}"
    # The lookup failed (offline, blocked): let Chrome follow the redirect itself.
    return SEARCH.format(quote_plus(phrase)), f"search redirect for {phrase!r}"
