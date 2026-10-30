"""Interactive Jev: every line you type goes straight to Jev, in your real Chrome.

No chat model reads the line first. The start page comes from the line itself (start.py);
a line that names no site continues on the page the last run ended on, so follow-ups work:

    jev > open youtube, search mkbhd and open his iPhone 17 review
    jev > turn on captions

One Chrome tab stays warm for the whole session, so only the first line pays for the tab.
"""

import argparse
import os
import sys

from . import start
from .cli import DATA, Progress

HELP = """  Type a goal; it runs in your Chrome straight away. A line that names no site continues
  on the current page. Commands: :new (fresh tab)  :help  :quit   Ctrl+C stops a run."""


def _args(goal, url, mode):
    return argparse.Namespace(
        goal=goal, url=url, values={}, find="", expect={}, evidence_dir=str(DATA / "evidence"),
        replay_dir=None, mode=mode, collect_steps=None, plan=None, max_steps=500,
        max_seconds=900, screenshot="",
    )


def _read(prompt):
    try:
        return input(prompt)
    except EOFError:
        return None


def interactive(tab=None, mode=None):
    from .browser import Browser
    from .cli import run_once

    os.environ["JEV_TAB_MODE"] = tab or os.environ.get("JEV_TAB_MODE") or "foreground"
    os.environ.setdefault("JEV_MAX_STEPS", "500")
    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    prompt = "\033[1;35mjev\033[0m > " if color else "jev > "
    print("  Jev - your words go straight to the decision model. :help for commands.")
    browser = None
    while True:
        line = _read(prompt)
        if line is None:
            break
        goal = line.strip()
        if not goal:
            continue
        if goal in {":q", ":quit", ":exit", "exit", "quit"}:
            break
        if goal == ":help":
            print(HELP)
            continue
        if goal == ":new":
            browser = _drop(browser)
            print("  next goal opens a fresh tab")
            continue
        progress = Progress(sys.stdout)
        url, how = start.resolve(goal)
        if url is None and browser is None:
            print("  name a site first ('open <site>, ...') or include a URL")
            continue
        try:
            if browser is None:
                browser = Browser(url)
            elif url is not None:
                browser.navigate(url)
            if url is not None:
                print(f"  -> {url}  ({how})")
            else:
                url = browser.evaluate("location.href")
                print(f"  continuing on {url}")
            trail, error, _code = run_once(_args(goal, url, mode), browser=browser, on_state=progress)
            progress.finish(trail[-1] if trail else None, error)
        except KeyboardInterrupt:
            # Never retried: whatever already executed stays done; the tab shows where it stopped.
            print("\n  stopped")
        except Exception as error:
            # The warm tab may be gone (closed by hand, Chrome restarted): start fresh next time.
            print(f"  error: {type(error).__name__}: {error}")
            browser = _drop(browser)
    _drop(browser, close=False)
    return 0


def _drop(browser, close=True):
    """Forget the warm tab. Leaving it open on exit lets you see where the last run ended."""
    if browser is not None and close:
        try:
            browser.close()
        except Exception:
            pass
    return None

