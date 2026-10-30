"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values.

The same helper model also classifies a goal into a durable task mode and harvests notable
items from a collected page. All three calls share one bounded, JSON-only text call path.
"""

import json
import math
import os
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx

from . import cache
from .questions import (
    ABSTAIN,
    FAILURE_CLASS,
    GOAL_DONE,
    HARVEST_PICK,
    IRREVERSIBLE,
    MODE_CLASSIFY,
    NEXT_ACTION,
    REOBSERVE,
    STUCK,
    SUBGOAL,
    TARGET,
    TEXT_VALUE,
    TEXT_VALUES,
    TYPE_VALUE,
)

# httpx drops an idle pooled connection after 5 s by default, and a new one costs ~325 ms of
# TLS + HTTP/2 setup (measured 2026-09-24: 499 ms cold vs 173 ms warm to TypeSafe). Any
# decision more than 5 s after the last - a slow results page, a supervisor consult, the
# next MCP call - silently paid it again. TypeSafe kept idle connections for 90 s+ (probed).
KEEPALIVE_S = float(os.environ.get("JEV_KEEPALIVE_SECONDS", "120"))
DECIDE_URL = "https://api.typesafe.ai/v1/systemone"
CLIENT = httpx.Client(http2=True, timeout=25, limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))

# The decision call also sits inside the anti-loop deadline, so it is bounded too: with
# the shared 25 s client timeout one slow decision could overshoot a 5 s guarantee five
# times over. A decision that cannot be made inside the remaining budget is not made.
DECIDE_TIMEOUT = float(os.environ.get("JEV_DECIDE_TIMEOUT", "3"))

# Decision hedging (2026-09-23). Over 533 live decisions the answer came back in a median
# 286 ms, p99 760 ms, max 929 ms; the failures were a different population - requests that
# hung to the whole 3 s budget, twice in a row ending a Flights run as blocked. A decision
# is read-only, so when none has answered by DECIDE_HEDGE_S one identical request goes out
# on its own connection (a stalled pooled HTTP/2 connection would stall a hedge sharing it)
# and the first valid answer wins. Under 1% of decisions pay for a duplicate. 0 disables.
DECIDE_HEDGE_S = float(os.environ.get("JEV_DECIDE_HEDGE", "1.0"))
HEDGE_CLIENT = httpx.Client(http2=True, timeout=25,
                            limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
_HEDGE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jev-hedge")

# The text helper is called inside the loop's anti-loop deadline (JEV_STALL_SECONDS,
# default 5 s). Its measured tail reaches 22 s, which would blow that guarantee four
# times over, so this call is bounded well below the deadline. A text call that cannot
# answer in time is a failed action, not a stalled run.
TEXT_TIMEOUT = float(os.environ.get("JEV_TEXT_TIMEOUT", "4"))

# The mode classifier runs before any page is touched, outside the anti-loop deadline, so a
# long goal may take longer than the in-loop helper budget. Without this ceiling a slow
# classifier call read as "classifier unavailable" and silently disabled mode creation.
CLASSIFY_TIMEOUT = float(os.environ.get("JEV_CLASSIFY_TIMEOUT", "12"))

# Stable per-process id for OpenCode Go's routing header; harmless for other providers.
TEXT_SESSION = os.environ.get("JEV_TEXT_SESSION") or uuid.uuid4().hex


def prewarm():
    """Open both decision connections while the browser is still starting.

    A fresh process otherwise pays the handshake inside its first decision. An unauthenticated
    HEAD carries no key and costs nothing; failures are ignored. Skipped without a TypeSafe
    key or with JEV_PREWARM=0 (the test default), so tests never touch the network.
    """
    if not os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_PREWARM", "1") == "0":
        return []
    return [_HEDGE_POOL.submit(_touch, client) for client in (CLIENT, HEDGE_CLIENT)]


def _touch(client):
    try:
        client.head(DECIDE_URL.rsplit("/v1/", 1)[0] + "/", timeout=3)
    except Exception:
        pass


def _typesafe_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        # A RuntimeError, like any other provider failure, so the loop ends the run with a
        # reason instead of escaping with no status.
        raise RuntimeError("TYPESAFE_API_KEY is not set; no action executed.")
    return key


def post_json(url, key, body, headers=None, timeout=None, client=None):
    """POST with a TOTAL time budget, not a per-attempt one.

    `timeout` used to bound each of up to three attempts, so a call given 3 s could occupy
    10.2 s (three ~2.9 s attempts plus backoff sleeps) - measured. Every caller that sized
    this against a remaining deadline was therefore wrong by more than 3x. The budget now
    covers the whole call, including retries.
    """
    budget = float(timeout or 25.0)
    deadline = time.monotonic() + budget
    for attempt in range(3):
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise RuntimeError(f"Model call exceeded its {budget:g}s budget; no action executed.")
        try:
            response = (client or CLIENT).post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {key}", **(headers or {})},
                timeout=min(left, budget),
            )
        except httpx.TimeoutException:
            raise RuntimeError(
                f"Model call exceeded its {budget:g}s budget; no action executed."
            ) from None
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            backoff = 0.5 * 2**attempt
            if time.monotonic() + backoff >= deadline:
                raise RuntimeError(
                    f"Model unavailable within its {budget:g}s budget; no action executed."
                )
            time.sleep(backoff)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def post_hedged(url, key, body, timeout=None, hedge_after=None):
    """post_json for a READ-ONLY call, hedged once. Returns (json, hedged).

    `hedged` is None when the first request answered before `hedge_after`, else "primary" or
    "hedge" for whichever answered first. The loser finishes in the background inside its
    own budget and its answer is dropped. Never use this for a call with side effects.
    """
    budget = float(timeout or 25.0)
    hedge_after = DECIDE_HEDGE_S if hedge_after is None else float(hedge_after)
    if hedge_after <= 0 or hedge_after >= budget - 0.2:
        return post_json(url, key, body, timeout=budget), None
    deadline = time.monotonic() + budget
    primary = _HEDGE_POOL.submit(post_json, url, key, body, timeout=budget)
    if wait([primary], timeout=hedge_after).done:
        return primary.result(), None
    hedge = _HEDGE_POOL.submit(post_json, url, key, body,
                               timeout=max(0.1, deadline - time.monotonic()), client=HEDGE_CLIENT)
    pending = {primary: "primary", hedge: "hedge"}
    failure = None
    while pending:
        done, _ = wait(pending, timeout=max(0.0, deadline - time.monotonic()),
                       return_when=FIRST_COMPLETED)
        if not done:
            break
        for future in done:
            name = pending.pop(future)
            try:
                return future.result(), name
            except RuntimeError as error:
                failure = error          # the other request may still answer
    if failure is not None and not pending:
        raise failure
    raise RuntimeError(f"Model call exceeded its {budget:g}s budget; no action executed.")


def text_call(system, context, timeout=None, model=None, base_url=None, max_tokens=1024,
              ceiling=None):
    """One bounded, JSON-only call to the text helper.

    Callers decide how to degrade: field_text turns a failure into a failed action, the
    classifier and the harvest extractor return None. The model and endpoint can be
    overridden per mode. `ceiling` is the most this call may ever take: in-loop calls keep
    the helper timeout, while the off-loop classifier may ask for more.
    """
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TEXT_MODEL_API_KEY is required for the text helper.")
    limit = float(ceiling or TEXT_TIMEOUT)
    if timeout is not None:
        limit = min(float(timeout), limit)
    base = (base_url or os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("TEXT_MODEL_BASE_URL must be an http(s) URL.")
    model_name = model or os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        # The OpenAI-standard field. Measured 2026-09-23 on the OpenCode Go endpoint with
        # deepseek-v4.1-flash: {"reasoning": {"enabled": false}} was silently ignored (116-178
        # hidden reasoning tokens, median 2.6 s per field); reasoning_effort="none" produced
        # 0 reasoning tokens and a 1.6 s median.
        reasoning = {"reasoning_effort": "none"}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model_name,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context)},
            ],
        },
        headers={"x-opencode-session": TEXT_SESSION, "User-Agent": "AgentJevUltraFast/0.1"},
        timeout=limit,
    )
    output = result["choices"][0]["message"]["content"]
    if isinstance(output, str):
        output = json.loads(output)
    if not isinstance(output, dict):
        raise ValueError("Text helper returned non-object JSON.")
    return output, {
        "model": model_name,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }


def noul(answer):
    """A Noul answer's probability; 0.0 when absent or malformed."""
    try:
        value = float(answer["noul"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and 0 <= value <= 1 else 0.0


def validate_choice(answer, ids):
    """The contract check. Still all-or-nothing, but the failure says what was wrong."""
    problems = []
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        choice = answer["choice"]
        if choice not in ids:
            problems.append("chosen id was not offered")
        if set(probabilities) != set(ids):
            problems.append(f"probability keys differ by {len(set(probabilities) ^ set(ids))}")
        if not all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers):
            problems.append("a probability or confidence is out of range")
        total = sum(v for v in probabilities.values() if type(v) in (int, float))
        if abs(total - 1) >= 0.02:
            problems.append(f"probabilities sum to {total:.3f}")
        best = max(
            (v for v in probabilities.values() if type(v) in (int, float)), default=None
        )
        if best is None or probabilities.get(choice) is None or probabilities[choice] < best - 1e-6:
            problems.append("chosen id is not the argmax")
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"malformed answer ({type(exc).__name__})")
    if problems:
        raise ValueError(f"Invalid TypeSafe response ({'; '.join(problems)}); no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "seek": "SEEK"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded", "pressed")
                       if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


# Literal values the user wrote into the goal. Code finds the spans; Jev only chooses which
# one a field takes, in the same request as the operation. Measured 2026-09-24: typing "MKBHD"
# waited 2.6 s on the text model although the goal said "type in MKBHD" verbatim.
COMPOSE = "COMPOSE"
QUOTED = re.compile(
    r'"([^"\n]{1,200})"|“([^”\n]{1,200})”|(?:(?<=\s)|^)' + r"'([^'\n]{1,200})'(?=[\s,.;:!?]|$)"
)
LITERAL_VERB = re.compile(
    r"\b(?:type(?:\s+in)?|enter|search(?:\s+for)?|write|fill\s+in|saying|say|named|called|"
    r"reply(?:ing)?\s+with|put)\s+",
    re.IGNORECASE,
)
LITERAL_STOP = re.compile(
    r"[,;.!?\"“]|\s(?:and|then|in the|into|on|to|so|before|after|once|from|via)\b", re.IGNORECASE
)
LEAD_IN = re.compile(r"^(?:the\s+)?(?:word|words|text|phrase|term|query|message)\s+", re.IGNORECASE)
MAX_LITERALS = 6
MAX_LITERAL_PAIRS = 48


def goal_literals(goal):
    """Candidate exact values from the goal: quoted strings, then words after a typing verb."""
    found = []
    for match in QUOTED.finditer(goal):
        found.append(next(g for g in match.groups() if g is not None))
    for match in LITERAL_VERB.finditer(goal):
        # Each verb's own tail: "type in MKBHD, ... saying bango" yields both values.
        phrase = LITERAL_STOP.split(" " + goal[match.end():], maxsplit=1)[0].strip()
        phrase = LEAD_IN.sub("", phrase)
        if QUOTED.search(phrase) or phrase[:1] == "'" or any(q in phrase for q in '"“”'):
            continue                                  # a quoted value was taken above
        if 0 < len(phrase.split()) <= 8:
            found.append(phrase)
    unique = []
    for value in (v.strip() for v in found):
        if value and value.lower() not in {u.lower() for u in unique}:
            unique.append(value)
    return unique[:MAX_LITERALS]


def media_line(media):
    """The main video's state as one line, or "". Page text never shows a player's speed,
    fullscreen or mute state, so a goal like "watch at 1.5x" was otherwise judged blind."""
    if not isinstance(media, dict):
        return ""
    parts = []
    if isinstance(media.get("speed"), (int, float)):
        parts.append(f"speed {media['speed']:g}x")
    for key, on, off in (("fullscreen", "fullscreen", "not fullscreen"), ("paused", "paused", "playing"),
                         ("muted", "muted", "sound on"), ("captions", "captions on", "captions off")):
        if isinstance(media.get(key), bool):
            parts.append(on if media[key] else off)
    return f"[video: {', '.join(parts)}]" if parts else ""


def page_text(state):
    line = media_line(state.get("media"))
    return line + "\n" + state["text"] if line else state["text"]


def choose(state, goal, history, timeout=None, allow_reobserve=True, use_cache=True,
           model_id=None, mode_state=None, guidance=None, allow_back=True):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "SEEK": "Move a slider (for example a video timeline) to a value supplied from the goal.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        BACK="Go back to the previous page in the browser history.",
        DONE="Every requirement is visibly satisfied.",
        BLOCKED="No supported operation can progress.",
        # Escape hatches, mandatory in the CUA bounded-chooser contract: without a
        # "none of the above" a softmax always names a winner, and a low-confidence
        # click looks exactly like a confident one.
        REOBSERVE=REOBSERVE,
        ABSTAIN=ABSTAIN,
    )
    if not allow_reobserve:
        # The loop's own re-observe budget is spent, so the option is not offered.
        operations.pop("REOBSERVE", None)
    if not allow_back:
        # Nothing this run reached lies behind the current page: BACK could only leave the
        # task (to the blank new tab, or the user's previous page in an active tab).
        operations.pop("BACK", None)
    # Stable candidate order. Jev-style decisions are sensitive to the ORDER candidates are
    # presented in - an independent reimplementation reports a different answer on 119 of 139
    # Choice tasks depending on order. Enumeration order is not something we control: it is
    # whatever order the DOM walk happened to produce, so the same page could reach different
    # answers between runs. Sorting by the candidate's own text removes that input instead of
    # hoping the model ignores it.
    operations = dict(sorted(operations.items()))
    # A supervisor sub-goal steers the NEXT action only; goal_done/stuck/irreversible keep
    # judging the caller's goal, so reaching a sub-goal can never read as finishing the task.
    steer = {"subgoal": guidance, "subgoal_rule": SUBGOAL} if guidance else {}
    questions = {
        "operation": {
            "type": "choice", "criteria": operations,
            "instructions": {"goal": goal, **steer, "rules": NEXT_ACTION},
        },
        "goal_done": {"type": "noul", "instructions": {"goal": goal, "rules": GOAL_DONE}},
        "stuck": {"type": "noul", "instructions": {"goal": goal, "rules": STUCK}},
        # Rides the same request: no extra round trip for the risk gate.
        "irreversible": {"type": "noul", "instructions": {"goal": goal, "rules": IRREVERSIBLE}},
    }
    for operation, candidates in targets.items():
        # Same reasoning as `operations` above: order the candidates by their own text so the
        # question does not change shape between two views of the same page.
        ordered = sorted(
            candidates.items(),
            key=lambda item: (
                item[1].get("label") or "",
                item[1].get("role") or "",
                str(item[1].get("current_value", item[1].get("value", "")) or ""),
                item[0],
            ),
        )
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "pressed") if k in a},
                }
                for index, a in ordered
            },
            "instructions": {
                "goal": goal, **steer, "operation": operation, "rules": [NEXT_ACTION, TARGET],
            },
        }
    # One option per (field, literal) pair, so the answer names the field it is for: with
    # bare literals Jev had to guess which field the question meant, and typed a reply text
    # ("banger") into the search box 17 times (measured 2026-09-24).
    literals = goal_literals(goal) if "TYPE_TEXT" in targets else []
    fields = targets.get("TYPE_TEXT") or {}
    literal_criteria = {
        f"{index}:{n}": f'type "{value}" into [{index}] {field["label"]}'
        for index, field in fields.items() for n, value in enumerate(literals, 1)
    }
    literal_values = {f"{index}:{n}": value for index in fields for n, value in enumerate(literals, 1)}
    if len(literal_criteria) > MAX_LITERAL_PAIRS:
        literal_criteria = {}
    if literal_criteria:
        literal_criteria[COMPOSE] = "None of these pairs: the field's value must be composed."
        questions["type_value"] = {
            "type": "choice", "criteria": literal_criteria,
            "instructions": {"goal": goal, "rules": TYPE_VALUE},
        }
    body = {
        "model": model_id or os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {**{k: state[k] for k in ("url", "title")}, "text": page_text(state)},
            "elements": elements,
            # Mode memory is user-authored context for this task class. It is data, not
            # page instructions, and the decision contract still comes from the questions.
            **({"mode": mode_state} if mode_state else {}),
            # `recovery` is included so the model can see WHY an action was substituted.
            # Without it a guard could silently redirect the loop and the model had no way
            # to learn the list was exhausted and change strategy (search instead of scan).
            "recent_actions": [
                {k: h.get(k) for k in
                 ("action", "kind", "text", "outcome", "page_changed", "recovery", "scan")}
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    key = cache.key_for(body)
    result = None
    hedged = None
    stored = cache.load(key) if use_cache else None
    if stored is not None and (not _cached_heads_valid(stored, operations, targets)
                               or _abstains(stored, operations)):
        # A poisoned entry must not make the same state fail until its TTL expires. Neither
        # may an old refusal: ABSTAIN is the model's uncertainty on one call, and serving it
        # again from the cache turns one shaky reading into a permanent one (measured: a
        # suite test abstained with zero model calls on a page it had passed before).
        cache.delete(key)
        stored = None
    if stored is not None:
        result = stored
    else:
        result, hedged = post_hedged(
            DECIDE_URL,
            _typesafe_key(),
            body,
            timeout=min(timeout or DECIDE_TIMEOUT, DECIDE_TIMEOUT),
        )
    try:
        operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    except ValueError as error:
        raise ValueError(f"{error} [operation head]") from None
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        try:
            target_answer = validate_choice(
                result["answers"].get(operation.lower() + "_target", {}), targets[operation]
            )
        except ValueError as error:
            raise ValueError(f"{error} [{operation.lower()}_target head]") from None
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    candidate_map = {
        name: {index: action["id"] for index, action in candidates.items()}
        for name, candidates in targets.items()
    }
    candidate_map.update({name: {name: action["id"]} for name, action in controls.items()})
    candidate_map["BACK"] = {"BACK": "BACK"}
    decision = {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probability": probabilities.get(choice, 0.0),
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "goal_done": noul(result["answers"].get("goal_done", {})),
        "stuck": noul(result["answers"].get("stuck", {})),
        # The risk gate's input, asked in this same request.
        "irreversible": noul(result["answers"].get("irreversible", {})),
        "candidate_map": candidate_map,
        "raw_answers": result["answers"],
        "model": result.get("model"),
        # A cache hit spent no tokens, so it reports no usage; `cached` says why.
        "usage": result.get("usage", {}) if stored is None else {},
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "cached": stored is not None,
        "hedged": hedged,
        "cache_key": key,
        "request": body,
    }
    if operation == "TYPE_TEXT" and literal_criteria:
        try:
            value = validate_choice(result["answers"].get("type_value", {}), literal_criteria)
        except (ValueError, TypeError, AttributeError):
            value = None      # optional head: a malformed answer leaves the text model in charge
        # Used only when the pair names the very field the target head chose.
        if value and value["choice"] != COMPOSE and value["choice"].split(":")[0] == target:
            decision["literal"] = {"text": literal_values[value["choice"]],
                                   "confidence": value["confidence"]}
    if stored is None and operation != "ABSTAIN":
        # Store only after every head this decision can execute has validated.
        cache.store(key, result)
    return decision


def _cached_heads_valid(result, operations, targets):
    """Re-validate the heads a cached entry could execute, with the same rules as live."""
    try:
        answer = result["answers"]
        operation = validate_choice(answer.get("operation", {}), operations)["choice"]
        if operation in targets:
            validate_choice(answer.get(operation.lower() + "_target", {}), targets[operation])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _abstains(result, operations):
    try:
        return validate_choice(result["answers"].get("operation", {}), operations)["choice"] == "ABSTAIN"
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def field_context(goal, action, page, history, mode_state=None):
    context = {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }
    if mode_state:
        context["mode"] = mode_state
    return context


def field_text(context, timeout=None, model=None, base_url=None):
    if not os.environ.get("TEXT_MODEL_API_KEY"):
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    try:
        output, meta = text_call(TEXT_VALUE, context, timeout=timeout, model=model, base_url=base_url)
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, meta


# A batch draft costs more output tokens than one field, so it gets a little more room. It
# runs off the critical path (in parallel with Jev), so this ceiling is not a stall risk.
PREFETCH_TIMEOUT = float(os.environ.get("JEV_PREFETCH_TIMEOUT", "8"))


def field_values(goal, labels, page, mode_state=None, timeout=None, model=None, base_url=None):
    """Draft values for several fields in ONE text call: {label: text or None}, plus meta.

    Used to prefetch while Jev decides, so TYPE_TEXT rarely waits on the helper. Every key
    must be one of the requested labels and every value a short string or null; anything
    else invalidates the whole batch rather than being repaired.
    """
    # Labels are sent and matched in normalized form: real pages carry stray whitespace
    # ("Where to? ") that a model reasonably trims, and one trimmed key must not void the
    # batch. Results are keyed back to the exact observed label.
    by_norm = {" ".join(str(label).split()).casefold(): label for label in labels}
    context = {
        "goal": goal,
        "fields": [" ".join(str(label).split()) for label in labels],
        "page": {"title": page.get("title"), "text": (page.get("text") or "")[:6000]},
        **({"mode": mode_state} if mode_state else {}),
    }
    output, meta = text_call(TEXT_VALUES, context, timeout=timeout, model=model,
                             base_url=base_url, ceiling=PREFETCH_TIMEOUT)
    # {"values": {...}} is asked for; a bare mapping is the same answer (measured: the model
    # drops the wrapper on some real pages). Every key is still checked below.
    values = output.get("values") if set(output) == {"values"} else output
    if not isinstance(values, dict) or not values:
        raise ValueError("Text helper returned no valid batch of field values.")
    keyed = {}
    for key, value in values.items():
        label = by_norm.get(" ".join(str(key).split()).casefold())
        if label is None:
            raise ValueError("Text helper returned a value for a field that was not asked for.")
        keyed[label] = value
    clean = {}
    for label, value in keyed.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            clean[label] = None
        elif isinstance(value, str) and len(value) <= 2000:
            clean[label] = value
        else:
            raise ValueError("Text helper returned an invalid field value in a batch.")
    return clean, meta


FAILURE_CRITERIA = {
    "recoverable": "A different offered action or target can still progress toward the goal.",
    "still_loading": "The page is still loading, settling, or waiting on data; more time may change it.",
    "hard_wall": "No offered action can progress: sign-in wall, bot check, error page, or exhausted options.",
}


def classify_failure(state, goal, history, timeout=None):
    """Classify a dead streak as one bounded choice. None when it cannot answer.

    Only called after repeated no-progress actions, so a healthy run never pays for it.
    Any failure to answer returns None and leaves the caller's existing recovery in charge.
    """
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return None
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state.get(k) for k in ("url", "title", "text")},
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "outcome", "page_changed", "recovery")}
                for h in history[-6:]
            ],
        },
        "questions": {
            "failure_class": {
                "type": "choice",
                "criteria": FAILURE_CRITERIA,
                "instructions": {"goal": goal, "rules": FAILURE_CLASS},
            }
        },
    }
    started = time.perf_counter()
    try:
        result = post_json(
            "https://api.typesafe.ai/v1/systemone",
            key,
            body,
            timeout=min(timeout or DECIDE_TIMEOUT, DECIDE_TIMEOUT),
        )
        answer = validate_choice(result["answers"].get("failure_class", {}), FAILURE_CRITERIA)
    except Exception:
        return None
    return {
        "class": answer["choice"],
        "confidence": answer["confidence"],
        "probabilities": answer["probabilities"],
        "model": result.get("model"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def classify_mode(goal, url, existing, timeout=None):
    """One typed judgement: is this a durable task class worth its own mode?

    Returns {"durable": False}, a mode decision dict, or None when the helper cannot answer.
    None means no mode: a guessed namespace with guessed keywords is worse than none.
    """
    context = {"goal": goal, "url": url, "existing_modes": existing or []}
    try:
        output, _ = text_call(MODE_CLASSIFY, context, timeout=timeout, ceiling=CLASSIFY_TIMEOUT)
    except Exception:
        return None
    if not isinstance(output, dict):
        return None
    existing_slug = output.get("existing")
    if isinstance(existing_slug, str):
        allowed = {item.get("slug") for item in existing or []}
        return {"existing": existing_slug} if existing_slug in allowed else None
    durable = output.get("durable")
    if isinstance(durable, str):
        durable = durable.strip().lower() in {"true", "yes", "1"}
    if durable is False:
        return {"durable": False}
    if durable is not True:
        return None
    name, slug = output.get("name"), output.get("slug")
    if not isinstance(name, str) or not isinstance(slug, str):
        return None
    keywords = [k for k in (output.get("keywords") or []) if isinstance(k, str)]
    strategy = output.get("strategy")
    return {
        "durable": True,
        "slug": slug,
        "name": name,
        "description": str(output.get("description") or ""),
        "keywords": keywords[:8],
        "strategy": strategy if strategy in {"decide", "collect"} else "decide",
    }


def harvest_items(page_text, memory, instructions=None, timeout=None, model=None, base_url=None):
    """Pick notable items from one page. None when the helper cannot answer validly."""
    context = {
        "interests": memory or "",
        "extra_instructions": instructions or "",
        # A shorter window keeps the call inside the anti-loop budget; later content is
        # harvested after the collector scrolls to it.
        "page_text": (page_text or "")[:2500],
    }
    try:
        output, _ = text_call(
            HARVEST_PICK, context, timeout=timeout, model=model, base_url=base_url, max_tokens=1500
        )
    except Exception:
        return None
    if not isinstance(output, dict):
        return None
    items = output.get("items")
    if not isinstance(items, list):
        return None
    cleaned = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        title, note = item.get("title"), item.get("note")
        if not isinstance(title, str) or not (3 <= len(title.strip()) <= 200):
            continue
        cleaned.append({
            "title": title.strip(),
            "note": note.strip()[:300] if isinstance(note, str) else "",
        })
    return cleaned or None
