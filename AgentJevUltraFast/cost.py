"""Conservative, usage-backed task cost estimates."""

JEV_INPUT_USD_PER_MILLION = 0.042


def _int(usage, *keys):
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def summarize(state):
    jev_calls = [item.get("usage") or {} for item in state.get("decisions", [])
                 if not item.get("cached")]
    router = (state.get("router") or {}).get("usage") or {}
    if router:
        jev_calls.append(router)
    jev_counts = [_int(item, "input_tokens", "prompt_tokens") for item in jev_calls]
    text_counts = [_int(item.get("usage"), "input_tokens", "prompt_tokens")
                   for item in state.get("text_calls", [])
                   if item.get("model") not in {"supplied", "profile", "mode-memory", "replay", "error"}]
    known_jev = [count for count in jev_counts if count is not None]
    known_text = [count for count in text_counts if count is not None]
    jev_total = sum(known_jev) if len(known_jev) == len(jev_counts) else None
    return {
        "jev_input_tokens": jev_total,
        "jev_input_usd_estimate": (
            round(jev_total * JEV_INPUT_USD_PER_MILLION / 1_000_000, 8)
            if jev_total is not None else None
        ),
        "text_input_tokens": sum(known_text) if len(known_text) == len(text_counts) else None,
        "text_usd_estimate": None,
        "cache_hits": state.get("cache_hits", 0),
        "cache_misses": state.get("cache_misses", 0),
        "note": "Estimates cover reported Jev input tokens; text-model prices are not configured.",
    }
