"""Opt-in launcher for Browser Use Cloud V4 browsers driving the Jev CLI.

    BROWSER_USE_API_KEY=... uv run python -m AgentJevUltraFast.cloud_runner \
        --goal "open example.com and read the headline" --url URL [--proxy de] \
        [--timeout-seconds 300]

One run is one cloud browser and one Jev CLI child:

1. `browser_use_sdk.v4.BrowserUse` creates the cloud browser (proxy choice only; no hosted
   agent task - the Jev CLI drives the browser over the CDP URL it is handed).
2. The browser ID alone is written to an ignored recovery path before the child starts, so a
   launcher that dies still names the browser a human must stop. The CDP URL is never written
   and never printed.
3. The Jev task runner runs as a child with a unique `BU_NAME` and the session's `BU_CDP_WS` in its
   environment (never on the command line). Its Cloud API key variable is empty so the
   Jev CLI's `.env` loader cannot reintroduce that key. Child output is scrubbed.
4. `finally` stops the child's browser-harness daemon best-effort, then calls
   `client.browsers.stop(session.id)`. A failed stop is reported (scrubbed of the key and the
   CDP URL) and turns the exit code non-zero. Nothing is retried: a second create or a second
   child would repeat an uncertain browser mutation.

The V4 SDK supports `profile_id`, `proxy_country_code`, and `timeout` (minutes).
It is imported lazily so offline tests do not need the optional dependency.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path(os.environ.get("JEV_HOME") or Path.home() / ".jev-ultrafast")
REPO = Path(__file__).resolve().parent.parent
# A source checkout keeps ignored run data in artifacts/; an installed copy in JEV_HOME.
DATA = REPO / "artifacts" if (REPO / "pyproject.toml").is_file() else HOME

API_KEY_ENV = "BROWSER_USE_API_KEY"
CDP_ENV = "BU_CDP_WS"
BU_NAME_ENV = "BU_NAME"
DEFAULT_TIMEOUT_S = 300.0
# The child's own --max-seconds budget stays this far below the launcher's hard bound, so the
# CLI stops itself (and reports JSON) before the launcher has to kill it.
CHILD_BUDGET_GRACE_S = 30.0


def default_record_path():
    """The ignored recovery file: the browser ID, never the CDP URL."""
    return DATA / "cloud" / "browser.json"


def parse_proxy(raw):
    """`none` (or empty) means a direct browser; a two-letter code means a Browser Use proxy."""
    value = (raw or "").strip().lower()
    if value in ("", "none"):
        return None
    if not re.fullmatch(r"[a-z]{2}", value):
        raise ValueError(f"proxy must be 'none' or a two-letter country code, got {raw!r}")
    return value


def unique_bu_name(session_id):
    """One daemon, one tab per cloud browser, named after the created session."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id)).strip("-.")
    return f"jev-cloud-{slug}" if slug else f"jev-cloud-{os.urandom(4).hex()}"


def websocket_endpoint(cdp_url):
    """Accept the SDK's WS URL or resolve an HTTP DevTools base via /json/version."""
    scheme = urlsplit(cdp_url).scheme.lower()
    if scheme in ("ws", "wss"):
        return cdp_url
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported CDP URL scheme {scheme or '(none)'}")
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=15) as response:
        resolved = json.load(response)["webSocketDebuggerUrl"]
    if urlsplit(resolved).scheme.lower() not in ("ws", "wss"):
        raise ValueError("CDP discovery did not return a WebSocket URL")
    return resolved


def scrub(text, *secrets):
    """Remove known secrets from anything this module is about to print."""
    cleaned = str(text)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(str(secret), "[redacted]")
    return cleaned


def cli_command(goal, url="", max_seconds=None, expect=""):
    """Use the stable source-checkout task entry point across CLI refactors."""
    script = REPO / "run_task.py"
    command = [sys.executable, str(script), "--goal", goal]
    if url:
        command += ["--url", url]
    if max_seconds is not None:
        command += ["--max-seconds", str(int(max_seconds))]
    if expect:
        command += ["--expect", expect]
    return command


def record_browser(path, *, session_id, bu_name, proxy):
    """Write the recovery record before the child starts; the ID only, never the CDP URL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": str(session_id),
        "bu_name": bu_name,
        "proxy_country_code": proxy,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "A cloud browser with this ID may still be running; stop it with "
                "browser_use_sdk.v4 BrowserUse().browsers.stop(session_id). This file never "
                "holds the CDP URL or the API key.",
    }
    # Exclusive creation prevents another run from overwriting an active browser's ID.
    serialized = json.dumps(payload, indent=2)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(serialized)


def _make_client(api_key):
    """Import the SDK only when a cloud run is actually requested."""
    from browser_use_sdk.v4 import BrowserUse

    return BrowserUse(api_key=api_key)


def stop_daemon_best_effort(bu_name, *, timeout_s=15.0):
    """Best-effort stop of the child's browser-harness daemon, before the cloud browser stops.

    The installed Browser Harness exposes restart_daemon(name) as its shutdown operation.
    Run it in a separate interpreter so the launcher's own imports cannot bind a different
    BU_NAME. The Cloud SDK stop remains the billing boundary even if this cleanup fails.
    """
    script = (
        "from browser_harness.admin import restart_daemon\n"
        "import os\n"
        "restart_daemon(os.environ['BU_NAME'])\n"
    )
    try:
        done = subprocess.run([sys.executable, "-c", script],
                              env={**os.environ, BU_NAME_ENV: str(bu_name)},
                              capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _run_child(command, child_env, timeout_s, err, secrets):
    """Run Jev; scrub its output before forwarding to the console."""
    try:
        completed = subprocess.run(command, env=child_env, cwd=str(REPO), timeout=timeout_s,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace")
    except subprocess.TimeoutExpired as exc:
        for stream, target in ((exc.stdout, sys.stdout), (exc.stderr, err)):
            if stream:
                value = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream
                print(scrub(value, *secrets), end="", file=target)
        print(f"cloud child exceeded its {timeout_s:g}s budget and was killed", file=err)
        return 2
    except OSError as exc:
        print(f"cloud child could not start: {type(exc).__name__}: "
              f"{scrub(exc, *secrets)}", file=err)
        return 1
    if completed.stdout:
        print(scrub(completed.stdout, *secrets), end="", file=sys.stdout)
    if completed.stderr:
        print(scrub(completed.stderr, *secrets), end="", file=err)
    code = int(completed.returncode or 0)
    if code != 0:
        print(f"the Jev CLI child exited {code}", file=err)
    return code


def run_cloud(goal, url="", *, proxy=None, profile="", expect="", timeout_s=DEFAULT_TIMEOUT_S,
              record_path=None, environ=None, client=None, client_factory=None,
              daemon_stopper=stop_daemon_best_effort, err=None):
    """One cloud browser, one Jev CLI child, and a guaranteed stop attempt.

    Returns the child's exit code (2 for a blocked precondition, 1 for a launcher error), or a
    non-zero code when the browser could not be stopped. `client` (a BrowserUse-like object)
    and `daemon_stopper` are injectable so the offline tests never touch the SDK.
    """
    env = dict(os.environ if environ is None else environ)
    err = err or sys.stderr
    record = Path(record_path) if record_path else default_record_path()

    api_key = (env.get(API_KEY_ENV) or "").strip()
    if not api_key:
        print(f"{API_KEY_ENV} is not set: cloud runs are opt-in, so nothing was started", file=err)
        return 2
    if not str(goal).strip():
        print("a goal is required", file=err)
        return 2
    if not str(url).strip():
        print("a start URL is required for a cloud run", file=err)
        return 2
    try:
        proxy_country = parse_proxy(proxy)
    except ValueError as exc:
        print(str(exc), file=err)
        return 2
    if not 0 < timeout_s <= 14400:
        print("timeout must be between 0 and 14400 seconds", file=err)
        return 2
    if record.exists():
        print(f"cloud recovery record already exists at {record}; resolve its browser before "
              "starting another run", file=err)
        return 2

    if client is None:
        factory = client_factory or _make_client
        try:
            client = factory(api_key)
        except Exception as exc:
            print(f"cloud client construction failed: {type(exc).__name__}: "
                  f"{scrub(exc, api_key)}", file=err)
            return 1

    session = None
    session_id = None
    bu_name = None
    cdp_url = None
    cdp_ws = None
    stop_error = None
    code = 1
    try:
        try:
            create_options = {
                # Explicit None disables the default residential proxy and its $5/GB rate.
                "proxy_country_code": proxy_country,
                # V4 precharges this lifetime, then refunds unused minutes when stopped.
                "timeout": min(240, max(1, math.ceil(timeout_s / 60))),
            }
            if str(profile).strip():
                create_options["profile_id"] = str(profile).strip()
            session = client.browsers.create(**create_options)
        except Exception as exc:
            print(f"cloud browser creation failed: {type(exc).__name__}: "
                  f"{scrub(exc, api_key)}", file=err)
            return 1
        session_id = getattr(session, "id", None)
        cdp_url = getattr(session, "cdp_url", None)
        if not session_id:
            print("cloud session has no id: nothing to attach to or stop", file=err)
            return 1
        if not isinstance(cdp_url, str) or not cdp_url:
            print(f"cloud browser {session_id} returned no CDP URL; stopping it", file=err)
            return 1
        try:
            cdp_ws = websocket_endpoint(cdp_url)
        except Exception as exc:
            print(f"cloud browser {session_id} CDP discovery failed: {type(exc).__name__}: "
                  f"{scrub(exc, api_key, cdp_url)}", file=err)
            return 1
        bu_name = unique_bu_name(session_id)
        try:
            record_browser(record, session_id=session_id, bu_name=bu_name, proxy=proxy_country)
        except OSError as exc:
            print(f"cloud recovery record could not be written ({type(exc).__name__}); "
                  "the child was not started", file=err)
            return 1
        child_env = dict(env)
        # CLI.load_dotenv() uses setdefault; an empty value prevents it reloading this key.
        child_env[API_KEY_ENV] = ""
        child_env[BU_NAME_ENV] = bu_name
        child_env[CDP_ENV] = cdp_ws
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        command = cli_command(goal, url,
                              max_seconds=max(1, int(timeout_s - CHILD_BUDGET_GRACE_S)),
                              expect=expect)
        secrets = [api_key, cdp_url, cdp_ws]
        secrets += [value for name, value in env.items() if name.upper().endswith("_API_KEY")]
        code = _run_child(command, child_env, timeout_s, err, secrets)
    finally:
        if session_id:
            if bu_name and daemon_stopper is not None:
                try:
                    daemon_stopper(bu_name)
                except Exception as exc:
                    print(f"browser daemon stop failed (ignored, the cloud stop still runs): "
                          f"{type(exc).__name__}: {scrub(exc, cdp_url, cdp_ws)}", file=err)
            try:
                client.browsers.stop(session_id)
            except Exception as exc:
                stop_error = exc
                print(f"cloud browser {session_id} stop failed: {type(exc).__name__}: "
                      f"{scrub(exc, api_key, cdp_url, cdp_ws)}", file=err)
                print(f"stop cloud browser {session_id} before any retry; this run's recovery "
                      f"record is {record}", file=err)
            else:
                print(f"cloud browser {session_id} stopped", file=err)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                print(f"cloud client close failed: {type(exc).__name__}: "
                      f"{scrub(exc, api_key, cdp_url, cdp_ws)}", file=err)
    if stop_error is not None:
        return code if code != 0 else 1
    if session_id:
        try:
            record.unlink(missing_ok=True)
        except OSError:
            pass
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m AgentJevUltraFast.cloud_runner",
        description="Run one Jev CLI task on an opt-in Browser Use Cloud V4 browser. "
                    "The cloud browser is always stopped when the run ends.")
    parser.add_argument("--goal", required=True, help="One natural-language goal for the Jev CLI.")
    parser.add_argument("--url", required=True, help="Start URL for the Cloud browser.")
    parser.add_argument("--proxy", default="none", metavar="none|CC",
                        help="'none' for a direct cloud browser, or a two-letter country code "
                             "for a Browser Use proxy.")
    parser.add_argument("--profile", default="",
                        help="Optional Browser Use Cloud profile ID for a saved login. It is "
                             "separate from Jev's local profile.")
    parser.add_argument("--expect", default="",
                        help="Jev CLI assertions as JSON or @path, read from the final live page. "
                             "A DONE without this has outcome unknown.")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"Whole-child budget; the child is killed past it and the browser "
                             f"is still stopped (default {DEFAULT_TIMEOUT_S:g}s).")
    parser.add_argument("--record", default=str(default_record_path()),
                        help="Ignored recovery file holding the browser ID before the child "
                             "starts; it never holds the CDP URL.")
    args = parser.parse_args(argv)
    try:
        proxy = parse_proxy(args.proxy)
    except ValueError as exc:
        parser.error(str(exc))
    return run_cloud(args.goal, args.url, proxy=proxy, profile=args.profile, expect=args.expect,
                     timeout_s=args.timeout_seconds, record_path=Path(args.record))


if __name__ == "__main__":
    sys.exit(main())
