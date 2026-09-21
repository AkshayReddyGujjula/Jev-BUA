"""Disk cache for a decision: `(model, questions, state)` in, the same typed answers out.

Two measured facts shape this. A decision is a pure function of its full request - the
entire body is the key, so candidate identity and order are part of the key by
construction, and a changed page, changed history or changed criteria set is a different
key. And repeats are frequent in an agent loop (revisiting a page, back-navigation,
recovering to the same state), so a hit is ~0 ms and $0 against a ~180 ms paid round trip.

Safety rules, from the knowledge base's caching-and-repeats pattern:
- never serve a cached decision across a mutation: the mutation is part of `recent_actions`
  and the page fingerprint is part of the request, so the key changes;
- a hit still runs through the same validation as a live answer;
- entries expire, and a malformed entry is a miss.

`JEV_CACHE=0` disables it; `JEV_CACHE_DIR` moves the store; `JEV_CACHE_TTL_S` sets expiry.
Disk cache is the default because most repeats are ACROSS runs (the same flow re-tested
after an edit), which an in-process cache cannot see.
"""

import hashlib
import json
import os
import time
from pathlib import Path

DEFAULT_TTL_S = 7 * 24 * 3600


def enabled():
    return os.environ.get("JEV_CACHE", "1").strip().lower() not in {"0", "off", "no", "false"}


def ttl_s():
    try:
        return max(0.0, float(os.environ.get("JEV_CACHE_TTL_S", str(DEFAULT_TTL_S))))
    except ValueError:
        return float(DEFAULT_TTL_S)


def directory():
    override = os.environ.get("JEV_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "jev-ultrafast" / "decisions"


def key_for(body):
    """The whole request body, canonicalised. Nothing about the decision is left out."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load(key):
    if not key or not enabled():
        return None
    path = directory() / f"{key}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        stored_at = float(entry["stored_at"])
        if time.time() - stored_at > ttl_s():
            path.unlink(missing_ok=True)
            return None
        result = entry["result"]
        if not isinstance(result, dict) or "answers" not in result:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(key, result):
    if not key or not enabled() or not isinstance(result, dict):
        return
    path = directory() / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace: a crash mid-write leaves the previous entry, not a half file.
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps({"stored_at": time.time(), "result": result}), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def delete(key):
    if not key:
        return
    try:
        (directory() / f"{key}.json").unlink(missing_ok=True)
    except OSError:
        pass
