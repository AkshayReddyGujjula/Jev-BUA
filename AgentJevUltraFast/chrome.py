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


ENV_FILES = (Path(__file__).resolve().parents[1] / ".env", Path.home() / ".jev-ultrafast" / ".env")


def _load_env_files():
    """Pull JEV_CHROME* settings from the .env files every entry point reads, before they do.

    apply_env runs when the package is imported, which is earlier than the CLI, the MCP server
    or the check runner load .env. Without this, JEV_CHROME=dedicated written in .env was
    silently ignored and runs attached to the user's own Chrome. The process env still wins.
    """
    for path in ENV_FILES:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            key = key.strip()
            if sep and key.startswith("JEV_CHROME") and not key.startswith("#"):
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


UNATTENDED_COMMANDS = {"test", "draft"}


def unattended_command(argv=None):
    """True when this process is `jev test|draft` (console script or `-m AgentJevUltraFast.cli`)."""
    argv = sys.argv if argv is None else argv
    if len(argv) < 2 or argv[1] not in UNATTENDED_COMMANDS:
        return False
    program = Path(argv[0]).name.lower()
    return program.startswith("jev") or program in {"cli.py", "-m", "-c"} or argv[0].endswith(
        ("AgentJevUltraFast\\cli.py", "AgentJevUltraFast/cli.py"))


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
    _load_env_files()
    if "JEV_CHROME" not in os.environ and unattended_command():
        # `jev test` / `jev draft` run unattended by design. Deciding here, at import, is the
        # only safe place: by the time the command's own code runs, Browser Harness has already
        # read BU_NAME, and a late switch once attached to the user's Chrome and waited forever
        # on its "Allow remote debugging?" prompt (observed 30 Oct 2026, 23:05).
        os.environ["JEV_CHROME"] = "dedicated"
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
