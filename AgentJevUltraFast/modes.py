"""Task-class modes: per-mode memory, learned values, specialists and harvest workspace.

A mode is a folder under JEV_MODES_DIR (default: ~/.jev-ultrafast/modes):

    <slug>/mode.json      identity, keywords, strategy, specialist knobs
    <slug>/AGENTS.md      human-editable memory: about me / standing instructions / interests
    <slug>/learned/       machine-written field label -> value shards, one file per process
    <slug>/harvest/       collected items, one JSONL file per day + seen.json dedupe index
    <slug>/replay/        per-mode descriptor replay plans

Resolution order: an explicit slug, then keyword matching against existing modes, then a
stored route, then one classifier call to the text model which may create a new mode (only
when it says the task is durable) or decide the task is a one-off.

Design rules, enforced here:

- Slugs from a model are untrusted. They are sanitized, never used as paths, and reads
  that resolve outside the mode root are refused.
- Mode files are local, gitignored data. Well, the root defaults outside the repository;
  a root inside a repo is reported so the operator can move it.
- Sensitive labels (passwords, OTPs, card numbers) are never learned and never injected.
- Learned values are only typed on an exact normalized label match, and only for fill
  actions; anything fuzzier goes back to the text model, which sees the memory instead.
- Harvested items only count when the title appears in the page text this run observed.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SENSITIVE_RE = re.compile(
    r"(password|passcode|passwd|one[- ]?time|otp|verification code|security code|"
    r"recovery code|backup code|auth(?:enticator| code)|2fa|two[- ]?factor|"
    r"cvv|cvc|card number|credit card|ssn|social security|secret|api key|access token|"
    r"passport|iban|national insurance|bank account|routing number|sort code|seed phrase|"
    r"private key|(?<![a-z0-9])pin(?![a-z0-9])|(?<![a-z0-9])code(?![a-z0-9]))",
    re.IGNORECASE,
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
MATCH_NORM_RE = re.compile(r"[\s\u200b-\u200f\ufeff]+")

MEMORY_MAX = 1500
TEXT_MEMORY_MAX = 3500
LEARNED_MAX = 25
VALUE_MAX = 2000
TITLE_MAX = 200
NOTE_MAX = 300
KEYWORDS_MAX = 8
ROUTES_MAX = 500
GENERAL_TTL_S = 86400
SEEN_MAX = 5000
# Keywords too broad to route on alone; the classifier is told to avoid them, and this is
# the code-side backstop.
KEYWORD_STOPWORDS = {
    "apply", "form", "page", "site", "website", "task", "the", "and", "for", "with",
    "my", "me", "user", "browser",
}


def modes_root():
    raw = os.environ.get("JEV_MODES_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jev-ultrafast" / "modes"


def mode_setting(requested=None):
    """The requested mode or the JEV_MODES default: off | auto | slug."""
    value = requested if requested is not None else os.environ.get("JEV_MODES")
    value = (value or "off").strip()
    return value or "off"


def enabled(value):
    return value.strip().lower() not in {"", "0", "off", "false", "no"}


def sanitize_slug(text):
    """A safe slug or None. Model output is never trusted as a path component."""
    lowered = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    lowered = lowered[:40].strip("-")
    return lowered if lowered and SLUG_RE.match(lowered) else None


def normalize_label(label):
    """Form labels normalized the way a person would read them: case, markers, spaces."""
    text = (label or "").lower().strip()
    text = re.sub(r"\(required\)", "", text)
    text = text.strip(" *:?\u200b")
    return " ".join(text.split())


def normalize_match(text):
    return MATCH_NORM_RE.sub(" ", (text or "").lower()).strip()


def is_sensitive(label):
    return bool(SENSITIVE_RE.search(label or ""))


def origin(url):
    """Canonical web origin for learned values; invalid and legacy URLs fail closed."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        port = parts.port
        default = 80 if parts.scheme == "http" else 443
        suffix = f":{port}" if port is not None and port != default else ""
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme}://{host}{suffix}"
    except (TypeError, ValueError):
        return None


def redact_for_replay(label, text):
    """Typed text safe to persist in a replay plan. Credentials never are."""
    if text is None or is_sensitive(label):
        return None
    return text


def in_repo(path):
    """True when the mode root sits inside a git worktree."""
    current = Path(path).resolve()
    for _ in range(12):
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent
    return False


def route_key(goal, url):
    return hashlib.sha256(f"{goal}\n{url}".encode("utf-8")).hexdigest()[:16]


def route_get(root, key):
    try:
        data = _read_json(Path(root) / "routes.json") or {}
        entry = (data.get("routes") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        slug = entry.get("slug")
        if not isinstance(slug, str):
            return None
        if slug == "general" and time.time() - float(entry.get("at") or 0) > GENERAL_TTL_S:
            return None
        return slug
    except (TypeError, ValueError, OSError):
        # A corrupt routes cache is treated as absent, never as a run failure.
        return None


def route_put(root, key, slug):
    try:
        path = Path(root) / "routes.json"
        data = _read_json(path) or {"version": 1, "routes": {}}
        routes = data.setdefault("routes", {})
        routes[key] = {"slug": slug, "at": time.time()}
        if len(routes) > ROUTES_MAX:
            for stale in sorted(
                routes, key=lambda k: float((routes[k] or {}).get("at") or 0)
            )[: len(routes) - ROUTES_MAX]:
                routes.pop(stale, None)
        return _write_json(path, data)
    except (TypeError, ValueError, OSError):
        return False


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError:
        return False


def source_match(items, page_text):
    """Keep only items whose title really appears in the page text this run saw.

    A harvested title the model paraphrased is not evidence of anything on the page, so it
    is dropped here rather than stored and later counted by harvest_at_least.
    """
    page = normalize_match(page_text)
    kept = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title or len(title) > TITLE_MAX:
            continue
        wanted = normalize_match(title)
        if len(wanted) < 4:
            continue
        if len(wanted) >= 12 and wanted in page:
            kept.append(item)
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(wanted) + r"(?![a-z0-9])", page):
            kept.append(item)
    return kept


class Mode:
    """One mode folder. Construction never raises for a partially written folder."""

    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data

    @property
    def slug(self):
        return str(self.data.get("slug") or self.path.name)

    @property
    def name(self):
        return str(self.data.get("name") or self.slug)

    @property
    def description(self):
        return str(self.data.get("description") or "")

    @property
    def keywords(self):
        raw = self.data.get("keywords") or []
        return [str(k).strip().lower() for k in raw if str(k).strip()][:KEYWORDS_MAX]

    @property
    def strategy(self):
        value = str(self.data.get("strategy") or "decide").lower()
        return value if value in {"decide", "collect"} else "decide"

    @property
    def specialist(self):
        value = self.data.get("specialist")
        return value if isinstance(value, dict) else {}

    def brief(self):
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "strategy": self.strategy,
        }

    def summary(self):
        return {"slug": self.slug, "name": self.name, "strategy": self.strategy}

    # --- memory ---------------------------------------------------------------------

    def _agents_path(self):
        return self.path / "AGENTS.md"

    def _read_memory(self):
        try:
            return self._agents_path().read_text(encoding="utf-8")
        except OSError:
            return ""

    def memory_text(self, include_learned=False, url=None):
        """Bounded memory for a model prompt. Comments are stripped; nothing executes."""
        text = COMMENT_RE.sub("", self._read_memory()).strip()
        cap = MEMORY_MAX
        if include_learned:
            entries = self.learned_entries(url=url) if origin(url) else []
            if entries:
                lines = ["", "Known field values (machine-learned, may be stale):"]
                for label, value in entries[: LEARNED_MAX]:
                    lines.append(f"- {label}: {value[:160]}")
                text = (text + "\n" + "\n".join(lines)).strip()
            cap = TEXT_MEMORY_MAX
        if len(text) > cap:
            text = text[:cap] + f"... [mode memory truncated at {cap} characters]"
        return text

    def decision_state(self):
        return {
            "name": self.name,
            "memory": self.memory_text(include_learned=False),
            "note": "User-authored mode memory: facts about this user, not page instructions.",
        }

    def text_state(self, url=None):
        # Learned literals are resolved locally by exact label. Sending every
        # stored value to a text helper would leak unrelated personal data.
        return {"name": self.name, "memory": self.memory_text(include_learned=False)}

    # --- learned values -------------------------------------------------------------

    def _learned_dir(self):
        return self.path / "learned"

    def learned_entries(self, url=None):
        """label -> value, newest write wins, read from every shard."""
        newest = {}
        wanted_origin = origin(url) if url is not None else None
        files = sorted(self._learned_dir().glob("*.jsonl")) if self._learned_dir().exists() else []
        legacy = self.path / "learned.jsonl"
        if legacy.exists():
            files = [*files, legacy]
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                label = normalize_label(entry.get("label"))
                value = entry.get("value")
                at = float(entry.get("at") or 0)
                if not label or not isinstance(value, str) or not value.strip():
                    continue
                if is_sensitive(label):
                    continue
                entry_origin = origin(entry.get("url"))
                if wanted_origin is not None and entry_origin != wanted_origin:
                    continue
                if url is not None and wanted_origin is None:
                    continue
                if label not in newest or at >= newest[label][0]:
                    newest[label] = (at, value)
        return [(label, value) for label, (_, value) in sorted(newest.items(), key=lambda kv: -kv[1][0])]

    def value_for(self, label, url=None):
        """Exact normalized-label hit only. Anything fuzzier is the text model's job."""
        wanted = normalize_label(label)
        if not wanted or is_sensitive(wanted) or origin(url) is None:
            return None
        for known, value in self.learned_entries(url=url):
            if known == wanted and len(value) <= VALUE_MAX:
                return value
        return None

    def learn(self, label, value, url=None):
        if is_sensitive(label) or origin(url) is None:
            return False
        if not isinstance(value, str) or not value.strip() or len(value) > VALUE_MAX:
            return False
        entry = {
            "at": time.time(),
            "label": str(label or "")[:120],
            "value": value,
            "url": (url or "")[:300],
        }
        try:
            self._learned_dir().mkdir(parents=True, exist_ok=True)
            path = self._learned_dir() / f"{os.getpid()}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return False
        return True

    def forget(self, label):
        wanted = normalize_label(label)
        removed = 0
        if not wanted:
            return 0
        paths = list(self._learned_dir().glob("*.jsonl")) if self._learned_dir().exists() else []
        legacy = self.path / "learned.jsonl"
        if legacy.exists():
            paths.append(legacy)
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            kept = []
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if normalize_label(entry.get("label")) == wanted:
                    removed += 1
                else:
                    kept.append(line)
            if removed:
                try:
                    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
                    temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                    os.replace(temp, path)
                except OSError:
                    pass
        return removed

    def remember(self, fact):
        """Append a user fact under 'About me', de-duplicated. Rewrites the file atomically."""
        text = " ".join(str(fact or "").split())
        if not text or len(text) > 500:
            return False
        memory = self._read_memory()
        if text.lower() in memory.lower():
            return False
        bullet = f"- {text}"
        lines = memory.splitlines()
        heading = next(
            (i for i, line in enumerate(lines) if line.strip().lower() == "## about me"), None
        )
        if heading is None:
            lines = [*lines, "", "## About me", bullet]
        else:
            insert_at = heading + 1
            while insert_at < len(lines) and (
                not lines[insert_at].strip() or lines[insert_at].lstrip().startswith("<!--")
            ):
                insert_at += 1
            lines.insert(insert_at, bullet)
        memory = "\n".join(lines).rstrip() + "\n"
        try:
            self._agents_path().parent.mkdir(parents=True, exist_ok=True)
            temp = self._agents_path().with_name(f"AGENTS.md.{os.getpid()}.tmp")
            temp.write_text(memory, encoding="utf-8")
            os.replace(temp, self._agents_path())
        except OSError:
            return False
        return True

    # --- harvest --------------------------------------------------------------------

    def harvest_dir(self):
        return self.path / "harvest"

    def _seen_path(self):
        return self.harvest_dir() / "seen.json"

    def _load_seen(self):
        """Insertion-ordered hash -> first-seen time. Legacy list files are tolerated."""
        data = _read_json(self._seen_path()) or {}
        titles = data.get("titles")
        if isinstance(titles, dict):
            return {str(key): float(value or 0) for key, value in titles.items()}
        if isinstance(titles, list):
            return {str(key): 0.0 for key in titles}
        return {}

    def _save_seen(self, seen):
        if len(seen) > SEEN_MAX:
            # Drop the oldest first so which keys survive is deterministic.
            for key in sorted(seen, key=lambda item: seen[item])[: len(seen) - SEEN_MAX]:
                seen.pop(key, None)
        _write_json(self._seen_path(), {"version": 1, "titles": seen})

    def save_items(self, url, items):
        """Persist source-matched items, de-duplicated by normalized title."""
        if not items:
            return []
        seen = self._load_seen()
        fresh = []
        for item in items:
            title = str(item.get("title") or "").strip()
            note = str(item.get("note") or "").strip()
            key = hashlib.sha256(normalize_match(title).encode("utf-8")).hexdigest()
            if not title or key in seen:
                continue
            seen[key] = time.time()
            fresh.append({
                "at": time.time(),
                "url": (url or "")[:500],
                "title": title[:TITLE_MAX],
                "note": note[:NOTE_MAX],
            })
        if not fresh:
            return []
        try:
            self.harvest_dir().mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d")
            with (self.harvest_dir() / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
                for entry in fresh:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return []
        self._save_seen(seen)
        return fresh

    def harvest_count(self, since=None):
        total = 0
        files = sorted(self.harvest_dir().glob("*.jsonl")) if self.harvest_dir().exists() else []
        for path in files:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if since is None or float(entry.get("at") or 0) >= since:
                    total += 1
        return total

    def replay_dir(self):
        path = self.path / "replay"
        return path


def load(root, slug):
    safe = sanitize_slug(slug)
    if safe is None:
        return None
    path = Path(root) / safe
    if not path.is_dir():
        return None
    data = _read_json(path / "mode.json")
    if data is None:
        return None
    data.setdefault("slug", safe)
    return Mode(path, data)


def list_modes(root):
    root = Path(root)
    if not root.is_dir():
        return []
    modes = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        data = _read_json(child / "mode.json")
        if data is not None:
            data.setdefault("slug", child.name)
            modes.append(Mode(child, data))
    return modes


def match(goal, modes):
    """Highest keyword score wins; a tie is not a guess and goes to the classifier."""
    text = f" {goal.lower()} "
    best, winners = 0, []
    for mode in modes:
        score = 0
        for keyword in mode.keywords:
            if len(keyword) < 3 or keyword in KEYWORD_STOPWORDS:
                continue
            if re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text):
                score += 1
        if score > best:
            best, winners = score, [mode]
        elif score and score == best:
            winners.append(mode)
    return winners[0] if best and len(winners) == 1 else None


def create(root, decision, goal=""):
    """Create the mode folder atomically. Never copies goal text into user memory."""
    root = Path(root)
    slug = sanitize_slug(decision.get("slug")) or sanitize_slug(decision.get("name"))
    if slug is None:
        slug = "mode-" + hashlib.sha256(str(decision).encode("utf-8")).hexdigest()[:10]
    existing = load(root, slug)
    if existing is not None:
        raise ValueError(
            f"Mode slug {slug!r} already belongs to {existing.name!r}; "
            "select it explicitly or choose a distinct task-class slug"
        )
    name = str(decision.get("name") or slug)[:80]
    description = str(decision.get("description") or "")[:200]
    keywords = []
    for keyword in decision.get("keywords") or []:
        keyword = str(keyword).strip().lower()[:40]
        if len(keyword) >= 3 and keyword not in KEYWORD_STOPWORDS and keyword not in keywords:
            keywords.append(keyword)
    strategy = str(decision.get("strategy") or "decide").lower()
    if strategy not in {"decide", "collect"}:
        strategy = "decide"
    data = {
        "version": 1,
        "slug": slug,
        "name": name,
        "description": description,
        "keywords": keywords[:KEYWORDS_MAX],
        "strategy": strategy,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created_from_sha256": hashlib.sha256(str(goal or "").encode("utf-8")).hexdigest(),
        "specialist": {
            "operation_model": None,
            "text_model": None,
            "text_base_url": None,
            "decide_timeout_s": None,
            "max_steps": None,
            "screenshots": None,
            "irreversible_floor": None,
            "cache": True,
            "replay": strategy != "collect",
            "collect": {"steps": 8, "harvest": strategy == "collect", "instructions": None},
        },
    }
    working = root / f".creating-{slug}-{os.getpid()}"
    final = root / slug
    try:
        (working / "learned").mkdir(parents=True, exist_ok=True)
        (working / "harvest").mkdir(parents=True, exist_ok=True)
        (working / "mode.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (working / "AGENTS.md").write_text(_memory_skeleton(name, description), encoding="utf-8")
        os.rename(working, final)
    except OSError:
        try:
            for path in sorted(working.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            working.rmdir()
        except OSError:
            pass
        existing = load(root, slug)
        if existing is not None:
            return existing
        raise
    return Mode(final, data)


def _memory_skeleton(name, description):
    return f"""# Mode: {name}

{description}

## About me
<!-- Facts the agent may use when a form asks for them. One per line. -->

## Standing instructions
<!-- Rules this mode must follow, for example: Never submit without confirmation. -->

## Interests
<!-- For collecting modes: what is worth keeping, and what to skip. -->
"""


def resolve(goal, url="", requested=None, timeout=None, classify=None):
    """The mode for this run, creating one when the classifier says the task is durable.

    Returns (mode_or_none, info). Never raises for auto resolution - a broken classifier,
    an unwritable root or a corrupt folder all degrade to no mode; an explicit slug that is
    missing raises, because the caller named it.
    """
    root = modes_root()
    info = {"root": str(root), "root_in_repo": in_repo(root)}
    setting = mode_setting(requested)
    if not enabled(setting):
        return None, {**info, "enabled": False, "matched": "off"}
    if setting.lower() != "auto":
        mode = load(root, setting)
        if mode is None:
            raise ValueError(
                f"Mode {setting!r} was not found in {root}. "
                "Create it with --mode auto, or write the folder yourself."
            )
        return mode, {**info, "enabled": True, "matched": "explicit", "slug": mode.slug}

    known = list_modes(root)
    hit = match(goal, known)
    if hit is not None:
        return hit, {**info, "enabled": True, "matched": "keyword", "slug": hit.slug}

    key = route_key(goal, url)
    cached = route_get(root, key)
    if cached == "general":
        return None, {**info, "enabled": True, "matched": "cached-general"}
    if cached:
        mode = load(root, cached)
        if mode is not None:
            return mode, {**info, "enabled": True, "matched": "route", "slug": mode.slug}

    if classify is None:
        from . import model as model_module

        classify = model_module.classify_mode
    try:
        decision = classify(goal, url, [m.brief() for m in known], timeout)
    except Exception as error:
        return None, {**info, "enabled": True, "matched": "none",
                      "error": f"{type(error).__name__}: {error}"[:200]}
    if decision is None:
        # The classifier could not answer. Do not cache "general": a later run should get
        # another chance to recognise the task rather than inherit an outage.
        return None, {**info, "enabled": True, "matched": "none",
                      "error": "classifier unavailable"}
    if not decision.get("durable"):
        route_put(root, key, "general")
        return None, {**info, "enabled": True, "matched": "not-durable"}
    try:
        mode = create(root, decision, goal)
    except (OSError, ValueError) as error:
        # An unwritable modes root must not fail the run: degrade to no mode.
        return None, {**info, "enabled": True, "matched": "none",
                      "error": f"could not create the mode: {error}"[:200]}
    route_put(root, key, mode.slug)
    return mode, {**info, "enabled": True, "matched": "created", "slug": mode.slug,
                  "name": mode.name, "strategy": mode.strategy}


def remember(slug_or_mode, fact):
    mode = slug_or_mode if isinstance(slug_or_mode, Mode) else load(modes_root(), slug_or_mode)
    if mode is None:
        raise ValueError("--remember needs an existing mode (--mode <slug>)")
    return mode.remember(fact)


def forget(slug_or_mode, label):
    mode = slug_or_mode if isinstance(slug_or_mode, Mode) else load(modes_root(), slug_or_mode)
    if mode is None:
        raise ValueError("--forget needs an existing mode (--mode <slug>)")
    return mode.forget(label)
