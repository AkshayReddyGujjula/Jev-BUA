"""Local, password-free identity fields for delegated browser tasks.

Only explicit field labels are matched. An origin must be approved before its
first profile fill; passwords, payment details and other sensitive labels are
rejected even if somebody places them in a profile file.
"""

import json
import os
from pathlib import Path

from .modes import is_sensitive, normalize_label, origin


def profile_root():
    return Path(os.environ.get("JEV_PROFILE_DIR") or Path.home() / ".jev-ultrafast" / "profile").expanduser()


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return data


def _fields(data, source):
    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError(f"{source} needs a fields object")
    clean = {}
    for label, value in fields.items():
        norm = normalize_label(str(label))
        if not norm or is_sensitive(norm):
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            continue
        clean.setdefault(norm, set()).add(value)
    return clean


class ProfileStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else profile_root()
        identity = _read_json(self.root / "identity.json")
        accounts = _read_json(self.root / "accounts.json")
        self.identity = _fields(identity, "identity.json")
        self.accounts = {}
        for site, data in accounts.items():
            web_origin = origin(site)
            if web_origin is None or not isinstance(data, dict):
                continue
            self.accounts[web_origin] = _fields(data, f"accounts.json[{site!r}]")
        approved = _read_json(self.root / "approvals.json")
        origins = approved.get("origins", [])
        if not isinstance(origins, list):
            raise ValueError("approvals.json origins must be a list")
        self.approved = {site for site in origins if origin(site) == site}

    def value_for(self, url, label):
        site = origin(url)
        wanted = normalize_label(label)
        if site is None or not wanted or is_sensitive(wanted):
            return None
        matches = set(self.identity.get(wanted, ()))
        matches.update(self.accounts.get(site, {}).get(wanted, ()))
        return next(iter(matches)) if len(matches) == 1 else None

    def protected_values(self):
        values = set()
        for group in [self.identity, *self.accounts.values()]:
            for choices in group.values():
                values.update(choices)
        return sorted(values, key=len, reverse=True)

    def is_approved(self, url):
        return origin(url) in self.approved

    def approve(self, url):
        site = origin(url)
        if site is None:
            raise ValueError("Cannot approve a non-web origin")
        self.root.mkdir(parents=True, exist_ok=True)
        self.approved.add(site)
        target = self.root / "approvals.json"
        temporary = self.root / f".approvals-{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps({"origins": sorted(self.approved)}, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        return site
