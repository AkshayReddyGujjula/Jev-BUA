"""`jev draft`: a starter test suite for a running app from one page read and one cheap call.

The page is read once (its visible text and labelled controls), and the text model proposes a
handful of user journeys as JSON. Code - not the model - writes the Markdown, so the output always
parses. A check phrase the model could not have seen on the page is kept but marked for review: it
describes a later screen the model only predicted. Nothing runs until `jev test` is called, and only
the page's own readback ever decides a pass.
"""

import os
from pathlib import Path

from .model import text_call

SYSTEM = """You design end-to-end browser tests for a web app, as JSON.
You get the app's start URL, its visible text, its labelled controls, and optionally what the app is for.
Return {"title": str, "tests": [{"name": str, "steps": [{"goal": str, "see": [str], "not_see": [str]}]}]}.
Rules:
- 3 to 8 tests that a real user would care about: main navigation, each form (one happy path and one
  invalid or empty input), search/filter, empty and error states, settings that persist.
- A goal is ONE user intention in plain English using the controls' visible names, e.g. "Open Settings",
  "Sign up with the email not-an-email". Never selectors, code, coordinates or URLs of other sites.
- 'see' lists short exact texts expected on the page after that step (1-3 per step). Prefer texts that
  are already on the page or that the app clearly shows; keep each under 60 characters.
- 'not_see' lists texts that must not appear (error banners, the previous screen's heading). Optional.
- Never delete, pay, send email, or change real data unless the control obviously belongs to a demo.
- Literal input values go inside the goal in double quotes."""

MAX_CONTROLS = 80


def page_brief(page):
    controls = []
    for action in (page.get("actions") or [])[:MAX_CONTROLS]:
        label = str(action.get("label") or "").strip()
        if label and action.get("kind") in {"click", "fill", "select"}:
            controls.append(f"{action.get('kind')} {action.get('role') or ''}: {label[:80]}".strip())
    return {"url": page.get("url"), "title": page.get("title"),
            "text": (page.get("text") or "")[:4000], "controls": controls}


def to_markdown(plan, base_url, path, page_text):
    visible = (page_text or "").lower()
    title = str(plan.get("title") or "Drafted suite").strip()[:80]
    lines = [f"# {title} (draft)", "",
             "Drafted by `jev draft` from one read of the start page. 'predicted' checks quote "
             "text the drafter expected on a later screen: confirm them on the first run. 'weak' "
             "checks were already true before the step: replace them with what the step should "
             "change.", "", f"base_url: {base_url}", ""]
    for test in (plan.get("tests") or [])[:8]:
        steps = [s for s in (test.get("steps") or []) if isinstance(s, dict) and str(s.get("goal") or "").strip()]
        if not steps:
            continue
        lines += [f"## {str(test.get('name') or 'Untitled').strip()[:90]}", f"path: {path}", ""]
        for number, step in enumerate(steps[:6], 1):
            lines.append(f"{number}. {' '.join(str(step['goal']).split())[:240]}")
            for key, label in (("see", "see"), ("not_see", "not see")):
                for phrase in (step.get(key) or [])[:3]:
                    phrase = " ".join(str(phrase).split())[:80]
                    if not phrase:
                        continue
                    if key == "see" and phrase.lower() not in visible:
                        lines.append("   <!-- predicted: not on the start page -->")
                    elif key == "see":
                        # True before the step ran, so it cannot show the step did anything.
                        lines.append("   <!-- weak: already on the start page -->")
                    lines.append(f"   - {label}: {phrase}")
        lines.append("")
    return "\n".join(lines)


def draft(url, about="", out=None):
    """Read `url`, ask the text model for journeys, write a Markdown spec. Returns its path."""
    from urllib.parse import urlsplit

    from . import Browser

    browser = Browser(url)
    try:
        page = browser.observe(screenshot=False)
    finally:
        browser.close()
    context = {"app": about or None, **page_brief(page)}
    plan, meta = text_call(SYSTEM, context, timeout=60, max_tokens=2500, ceiling=60)
    parts = urlsplit(url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    markdown = to_markdown(plan, base_url, path, page.get("text"))
    target = Path(out or Path("jev-tests") / "drafted.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target, meta


def main(argv):
    import argparse

    from .checks import load_env

    parser = argparse.ArgumentParser(prog="jev draft", description=__doc__)
    parser.add_argument("url", help="the running app's start page, e.g. http://localhost:3000")
    parser.add_argument("--about", default="", help="one sentence on what the app is for")
    parser.add_argument("--out", default="", help="where to write the spec (default jev-tests/drafted.md)")
    args = parser.parse_args(argv)
    load_env()
    os.environ.setdefault("JEV_CHROME", "dedicated")
    target, meta = draft(args.url, args.about, args.out or None)
    usage = meta.get("usage") or {}
    print(f"wrote {target} ({meta['latency_ms']} ms, {usage.get('total_tokens', '?')} tokens)")
    print(f"review it, then run:  jev test {target}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
