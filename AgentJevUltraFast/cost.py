"""Conservative, usage-backed task cost estimates.

Every paid call a run makes is counted here: Jev decisions (plus hedged duplicates, which are
billed but return no extra answer), text-model drafts, the parallel reviewer and the
supervisor. Text-side dollars come from what the provider itself reports per call (OpenCode Go
returns a `list_cost`), never from a price table that could drift.
"""

JEV_INPUT_USD_PER_MILLION = 0.042
FREE_TEXT_SOURCES = {"supplied", "profile", "mode-memory", "replay", "error", "goal-literal", "supervisor"}


def _int(usage, *keys):
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _reported_usd(usage):
    """The provider's own dollar figure for one call, or None when it reported none."""
    if not isinstance(usage, dict):
        return None
    vendor = usage.get("opencode")
    if isinstance(vendor, dict) and isinstance(vendor.get("list_cost"), (int, float)):
        return float(vendor["list_cost"])
    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        return float(cost)
    return None


def summarize(state):
    decisions = state.get("decisions", [])
    jev_calls = [item.get("usage") or {} for item in decisions if not item.get("cached")]
    hedges = sum(1 for item in decisions if item.get("hedged") and not item.get("cached"))
    router = (state.get("router") or {}).get("usage") or {}
    if router:
        jev_calls.append(router)
    jev_counts = [_int(item, "input_tokens", "prompt_tokens") for item in jev_calls]
    paid_text = [item for item in state.get("text_calls", [])
                 if item.get("model") not in FREE_TEXT_SOURCES]
    # A batch draft's usage is copied onto every field it filled; count each batch once.
    seen, text_usage = set(), []
    for item in paid_text:
        usage = item.get("usage") or {}
        key = (item.get("model"), item.get("batch_id")) if item.get("batch_id") else id(item)
        if key in seen:
            continue
        seen.add(key)
        text_usage.append(usage)
    reviews = [item for item in state.get("reviews", []) if item.get("usage") or item.get("error") is None]
    supervision = state.get("supervision", [])
    side_usage = text_usage + [item.get("usage") or {} for item in reviews] \
        + [item.get("usage") or {} for item in supervision]
    known_jev = [count for count in jev_counts if count is not None]
    text_counts = [_int(item, "input_tokens", "prompt_tokens") for item in text_usage]
    known_text = [count for count in text_counts if count is not None]
    jev_total = sum(known_jev) if len(known_jev) == len(jev_counts) else None
    jev_usd = (round(jev_total * JEV_INPUT_USD_PER_MILLION / 1_000_000, 8)
               if jev_total is not None else None)
    reported = [_reported_usd(usage) for usage in side_usage]
    side_usd = round(sum(value for value in reported if value is not None), 8)
    return {
        "calls": {
            "jev": len(jev_calls),
            "jev_hedges": hedges,
            "jev_cached": sum(1 for item in decisions if item.get("cached")),
            "text": len(text_usage),
            "review": len(reviews),
            "supervisor": len(supervision),
        },
        "jev_input_tokens": jev_total,
        "jev_input_usd_estimate": jev_usd,
        "text_input_tokens": sum(known_text) if len(known_text) == len(text_counts) else None,
        "side_usd_reported": side_usd,
        "usd_estimate": round((jev_usd or 0.0) + side_usd, 8),
        "cache_hits": state.get("cache_hits", 0),
        "cache_misses": state.get("cache_misses", 0),
        "note": ("Jev: reported input tokens at $0.042/M (hedged duplicates excluded). Text, "
                 "review and supervisor: the provider's own per-call figure where it gives one."),
    }
