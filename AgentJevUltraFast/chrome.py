"""A dedicated automation Chrome, so unattended runs never wait on an approval dialog.

Attaching to the user's own Chrome (the default) makes Chrome 144+ show "Allow remote
debugging?" once per connection, and Browser Harness deliberately waits on that dialog with
no deadline. Nobody is there to click it during an overnight test run.

`JEV_CHROME=dedicated` instead launches (or reuses) a separate Chrome with its own profile and
a DevTools port, and points Browser Harness at it through BU_CDP_URL. A non-default profile with
`--remote-debugging-port` raises no dialog, and the harness bounds a CDP-URL startup at 60 s, so
a missing browser is an error, never a hang. The user's real Chrome, its tabs and its sign-ins are
never touched. Sign-ins made inside the dedicated profile persist in that profile.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9333
DEFAULT_PROFILE = Path.home() / ".jev-ultrafast" / "chrome-profile"
# Flags that keep a hidden or occluded tab rendering like a visible one, and keep Chrome from
# opening anything that waits on a person (first-run sheet, default-browser bar, keychain).
LAUNCH_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,MediaRouter",
    "--password-store=basic",
    "--window-size=1280,900",
)


def mode():
    value = os.environ.get("JEV_CHROME", "user").strip().lower()
    return "dedicated" if value in {"dedicated", "own", "separate"} else "user"


def dedicated():
    return mode() == "dedicated"


def port():
    return int(os.environ.get("JEV_CHROME_PORT", DEFAULT_PORT))


def endpoint():
    return f"http://127.0.0.1:{port()}"


def profile_dir():
    return Path(os.environ.get("JEV_CHROME_PROFILE") or DEFAULT_PROFILE)


def apply_env():
    """Route Browser Harness to the dedicated Chrome. Must run before browser_harness is imported.

    Browser Harness reads BU_NAME at import time and keeps one daemon per name, so the dedicated
    browser gets its own daemon name: a daemon already attached to the user's Chrome under the same
    name would otherwise be silently reused. An explicit BU_CDP_WS/BU_CDP_URL always wins.
    """
    if not dedicated() or os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL"):
        return
    os.environ["BU_CDP_URL"] = endpoint()
    suffix = f"-chrome{port()}"
    name = os.environ.get("BU_NAME") or "default"
    if not name.endswith(suffix):
        os.environ["BU_NAME"] = name + suffix


def find_chrome():
    configured = os.environ.get("JEV_CHROME_PATH")
    if configured:
        return configured
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for path in candidates:
        if path.exists():
            return str(path)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def version(timeout=1.0):
    """The DevTools /json/version document, or None when nothing answers on the port."""
    try:
        with urllib.request.urlopen(endpoint() + "/json/version", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def launch_command(executable):
    command = [executable, f"--remote-debugging-port={port()}", f"--user-data-dir={profile_dir()}",
               *LAUNCH_FLAGS]
    if os.environ.get("JEV_CHROME_HEADLESS", "0").strip().lower() in {"1", "true", "yes", "on"}:
        command.append("--headless=new")
    command.append("about:blank")
    return command


def ensure_chrome(timeout=None):
    """Make sure the dedicated Chrome answers on its port; launch it once if it does not.

    Returns the /json/version document. Raises RuntimeError (never blocks past `timeout`) when
    Chrome cannot be found or does not come up, so the caller reports it instead of hanging.
    """
    info = version()
    if info:
        return info
    executable = find_chrome()
    if not executable:
        raise RuntimeError("dedicated-chrome-missing: Chrome not found; set JEV_CHROME_PATH")
    profile_dir().mkdir(parents=True, exist_ok=True)
    flags = {}
    if sys.platform == "win32":
        flags["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    # Detached: the browser outlives this run so the next run reuses it warm.
    subprocess.Popen(launch_command(executable), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **flags)
    deadline = time.monotonic() + (timeout if timeout is not None
                                   else float(os.environ.get("JEV_CHROME_START_S", "20")))
    while time.monotonic() < deadline:
        info = version(timeout=0.5)
        if info:
            return info
        time.sleep(0.2)
    raise RuntimeError(
        f"dedicated-chrome-timeout: no DevTools endpoint on {endpoint()}; is another program using "
        f"port {port()}, or is profile {profile_dir()} open in a Chrome started without the port?")
