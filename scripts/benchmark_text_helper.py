"""Latency-first benchmark of the TYPE_TEXT helper: hosted DeepSeek vs small local models.

The question (2026-09-23): can a 2-4 GB local model replace the hosted text helper for form
values, and would it be FASTER? Latency is the primary metric; accuracy is a gate - a fast
helper that invents a phone number is disqualified, not "slightly worse".

Every provider gets the identical system prompt (questions.TEXT_VALUE) and the identical
context shape (model.field_context), so the comparison is the model, not the prompt. Two
context sizes are measured, because a local model's latency is dominated by prompt
processing: `full` is what the loop sends today (page text up to 6000 chars), `slim` keeps
only the goal, the field, memory and the page title.

Cases are a fictional applicant (no real personal data) on a synthetic application page:
- fact: the value is in mode memory and must be copied exactly;
- goal: the value is in the goal text;
- missing: the value exists nowhere, the only correct answer is null (hallucination check);
- free: short free text, judged by code (non-empty, names the company, 5-90 words).

    uv run --env-file .env python scripts/benchmark_text_helper.py \
        --providers deepseek ollama:qwen3.5:4b ollama:granite4.2:3b --repeats 3

No browser. Ollama runs natively (/api/chat, think=false, temperature 0, format=json).
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from AgentJevUltraFast.model import field_context, text_call  # noqa: E402
from AgentJevUltraFast.questions import TEXT_VALUE  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

MEMORY = """Applicant facts (the user's own, fictional for this benchmark):
- Full name: Ada Example
- Email: ada.example@example.org
- Phone: +44 7700 900123
- City: Manchester, United Kingdom
- University: University of Manchester
- Degree: BSc Computer Science
- Expected graduation: June 2027
- GPA / grade: First-class (predicted)
- LinkedIn: https://www.linkedin.com/in/ada-example
- GitHub: https://github.com/ada-example
- Work authorisation: UK citizen, no sponsorship required
- Interests: distributed systems, developer tooling"""

PAGE_TEXT = (
    "Acme Robotics - Software Engineering Intern (Summer 2027)\n"
    "Apply now. All fields marked * are required.\n"
    "Personal details: First name*, Last name*, Email*, Phone*, City*\n"
    "Education: University*, Degree*, Graduation date*, Grade\n"
    "Links: LinkedIn, GitHub, Portfolio\n"
    "Eligibility: Are you authorised to work in the UK?* Passport number*\n"
    "Tell us about yourself: Why do you want to join Acme Robotics? (2-3 sentences)*\n"
    "Salary expectation. National Insurance number.\n"
    + "About Acme Robotics. We build warehouse robots. " * 60
    + "\nEqual opportunities statement. Privacy notice. Cookie settings. " * 20
)
GOAL = "Fill in the Acme Robotics internship application for me using my profile. Do not submit."

CASES = [
    ("fact", "First name", "Ada"),
    ("fact", "Last name", "Example"),
    ("fact", "Email", "ada.example@example.org"),
    ("fact", "Phone", "+44 7700 900123"),
    ("fact", "University", "University of Manchester"),
    ("fact", "Graduation date", "June 2027"),
    ("fact", "LinkedIn profile URL", "https://www.linkedin.com/in/ada-example"),
    ("fact", "City", ("Manchester", "Manchester, United Kingdom")),
    ("missing", "Passport number", None),
    ("missing", "National Insurance number", None),
    ("missing", "Salary expectation", None),
    ("free", "Why do you want to join Acme Robotics?", "Acme"),
]
GOAL_CASES = [
    ("goal", "Search flights from Zurich to London on 20 September.", "From", "Zurich"),
    ("goal", "Search flights from Zurich to London on 20 September.", "Where to?", "London"),
    ("goal", "Find me 3 bedroom houses to rent in Leeds under 1500 a month.",
     "Maximum price", "1500"),
]


def contexts(slim):
    out = []
    page = {"title": "Apply - Acme Robotics", "text": "" if slim else PAGE_TEXT}
    mode = {"name": "internship-applications", "memory": MEMORY}
    for kind, label, expected in CASES:
        action = {"label": label, "role": "textbox", "value": ""}
        out.append((kind, label, expected, field_context(GOAL, action, page, [], mode_state=mode)))
    for kind, goal, label, expected in GOAL_CASES:
        action = {"label": label, "role": "combobox", "value": ""}
        gpage = {"title": "Search", "text": "" if slim else PAGE_TEXT[:1500]}
        out.append((kind, label, expected, field_context(goal, action, gpage, [])))
    return out


def norm(value):
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def judge(kind, expected, value):
    if kind == "missing":
        return value is None or norm(value) in {"", "null", "none", "n/a"}
    if value is None or not str(value).strip():
        return False
    if kind == "free":
        words = len(str(value).split())
        return expected.lower() in str(value).lower() and 5 <= words <= 90
    got = norm(value)
    if isinstance(expected, tuple):
        return got in {norm(e) for e in expected}
    want = norm(expected)
    return got == want or (kind == "goal" and want in got and len(got) <= len(want) + 12)


def call_ollama(model, context, timeout):
    started = time.perf_counter()
    response = httpx.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": model,
            "stream": False,
            "think": False,
            "format": "json",
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 160},
            "messages": [{"role": "system", "content": TEXT_VALUE},
                         {"role": "user", "content": json.dumps(context)}],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    latency = (time.perf_counter() - started) * 1000
    output = json.loads(body["message"]["content"])
    return output.get("text") if isinstance(output, dict) else None, latency, {
        "prompt_tokens": body.get("prompt_eval_count"),
        "prompt_ms": round((body.get("prompt_eval_duration") or 0) / 1e6),
        "gen_tokens": body.get("eval_count"),
        "gen_ms": round((body.get("eval_duration") or 0) / 1e6),
        "load_ms": round((body.get("load_duration") or 0) / 1e6),
    }


def call_hosted(context, timeout):
    started = time.perf_counter()
    output, meta = text_call(TEXT_VALUE, context, timeout=timeout, ceiling=timeout)
    return output.get("text"), (time.perf_counter() - started) * 1000, meta.get("usage", {})


def run_provider(provider, slim, repeats, timeout):
    cases = contexts(slim)
    cold = None
    if provider.startswith("ollama:"):
        model = provider.split(":", 1)[1]
        # Cold = first call after an explicit unload, so load time is measured, not guessed.
        try:
            httpx.post(f"{OLLAMA}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
        except httpx.HTTPError:
            pass
        _, cold, extra = call_ollama(model, cases[0][3], timeout=max(timeout, 120))
        cold = {"ms": round(cold), **extra}

        def call(ctx):
            return call_ollama(model, ctx, timeout)
    else:
        def call(ctx):
            return call_hosted(ctx, timeout)
    rows = []
    for rep in range(repeats):
        for kind, label, expected, ctx in cases:
            try:
                value, ms, extra = call(ctx)
                error = None
            except Exception as exc:          # a failure is a data point, not a crash
                value, ms, extra, error = None, None, {}, f"{type(exc).__name__}: {exc}"[:160]
            ok = error is None and judge(kind, expected, value)
            rows.append({"rep": rep, "kind": kind, "label": label, "ok": ok, "ms": ms,
                         "value": value if kind != "free" else (value or "")[:120],
                         "error": error, **({"meta": extra} if extra else {})})
    lat = [r["ms"] for r in rows if r["ms"] is not None]
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], [0, 0])
        k[0] += r["ok"]
        k[1] += 1
    return {
        "provider": provider,
        "context": "slim" if slim else "full",
        "cold": cold,
        "median_ms": round(statistics.median(lat)) if lat else None,
        "p90_ms": round(sorted(lat)[max(0, int(len(lat) * 0.9) - 1)]) if lat else None,
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 3),
        "by_kind": {k: f"{v[0]}/{v[1]}" for k, v in by_kind.items()},
        "hallucinated_missing": sum(1 for r in rows if r["kind"] == "missing" and not r["ok"]),
        "errors": sum(1 for r in rows if r["error"]),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--providers", nargs="+", default=["deepseek"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--contexts", nargs="+", default=["full", "slim"],
                        choices=["full", "slim"])
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "benchmark-text-helper.json"))
    args = parser.parse_args()
    if any(p == "deepseek" for p in args.providers):
        os.environ.setdefault("TEXT_MODEL_REASONING", "none")
    results = []
    for provider in args.providers:
        for ctx in args.contexts:
            print(f"== {provider} / {ctx}", flush=True)
            result = run_provider(provider, ctx == "slim", args.repeats, args.timeout)
            results.append(result)
            print(f"   cold={result['cold'] and result['cold']['ms']} ms  "
                  f"median={result['median_ms']} ms  p90={result['p90_ms']} ms  "
                  f"accuracy={result['accuracy']}  {result['by_kind']}  "
                  f"hallucinated_missing={result['hallucinated_missing']}  "
                  f"errors={result['errors']}", flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"results": results}, indent=2, default=str),
                              encoding="utf-8")
    print(f"report: {args.out}")


if __name__ == "__main__":
    main()
