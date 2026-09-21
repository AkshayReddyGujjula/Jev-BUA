"""Local trap pages for verifying the anti-loop deadline. Verification asset, not production.

Serves deliberately hostile pages so the guarantee "the loop never spins for more than
JEV_STALL_SECONDS" can be tested against real browser behaviour instead of a mock:

  /trap?mode=dead       a button that does nothing - the classic repeat-forever trap
  /trap?mode=ab         two pages linking to each other - an A/B oscillation
  /trap?mode=loading    readyState never reaches complete (a subresource hangs forever)
  /trap?mode=form       a working form, for the happy path and caller-supplied values
  /trap?mode=slow       a real button, but the result page takes several seconds

Usage:  python scripts/trap_server.py --port 8791
"""

import argparse
import http.server
import threading
import time
from urllib.parse import parse_qs, urlparse

STYLE = "font-family:system-ui;max-width:40rem;margin:3rem auto;line-height:1.6"

DEAD = f"""<!doctype html><html><head><title>Trap: dead control</title></head>
<body style="{STYLE}">
<h1>Account settings</h1>
<p>Confirm your changes to continue.</p>
<button id="confirm" type="button">Confirm changes</button>
<button id="cancel" type="button">Cancel</button>
<p id="status">Nothing has been confirmed yet.</p>
<script>
  // Both controls are inert on purpose: no handler can ever produce the success state.
  document.getElementById('confirm').addEventListener('click', function () {{}});
  document.getElementById('cancel').addEventListener('click', function () {{}});
</script>
</body></html>"""

AB_A = f"""<!doctype html><html><head><title>Step one of two</title></head>
<body style="{STYLE}">
<h1>Step one</h1>
<p>Go to step two to finish.</p>
<a id="next" href="/trap?mode=ab&step=2">Continue to step two</a>
<button id="back" type="button" onclick="history.back()">Back</button>
</body></html>"""

AB_B = f"""<!doctype html><html><head><title>Step two of two</title></head>
<body style="{STYLE}">
<h1>Step two</h1>
<p>Review is not available.</p>
<a id="prev" href="/trap?mode=ab&step=1">Return to step one</a>
<button id="finish" type="button" onclick="void 0">Finish</button>
</body></html>"""

LOADING = f"""<!doctype html><html><head><title>Trap: never finishes loading</title></head>
<body style="{STYLE}">
<h1>Dashboard</h1>
<p id="status">Loading your data…</p>
<img src="/never" alt="" width="1" height="1">
<button id="retry" type="button">Retry</button>
</body></html>"""

FORM = f"""<!doctype html><html><head><title>Search the catalogue</title></head>
<body style="{STYLE}">
<h1>Catalogue</h1>
<form id="search" method="get" action="/trap">
  <input type="hidden" name="mode" value="results">
  <label for="q">Search the catalogue</label>
  <input id="q" name="q" type="search" placeholder="Search the catalogue">
  <label for="sort">Sort by</label>
  <select id="sort" name="sort">
    <option value="relevance">Relevance</option>
    <option value="newest">Newest first</option>
    <option value="price">Price: low to high</option>
  </select>
  <button id="go" type="submit">Search</button>
</form>
</body></html>"""


def results_page(query, sort):
    return f"""<!doctype html><html><head><title>Results for {query}</title></head>
<body style="{STYLE}">
<h1>Results</h1>
<p id="applied">Query: {query} · Sort: {sort or 'relevance'}</p>
<ul><li>Result one</li><li>Result two</li></ul>
</body></html>"""


def list_page(count, target_index, target_label):
    """A long list whose target sits deep, so reaching it requires real scanning."""
    rows = []
    for i in range(1, count + 1):
        label = target_label if i == target_index else f"Catalogue item {i:02d} - an ordinary row"
        rows.append(
            f'<a id="item{i}" href="/trap?mode=picked&n={i}" '
            f'style="display:block;height:110px;padding:10px;border-bottom:1px solid #ddd">{label}</a>'
        )
    return (
        '<!doctype html><html><head><title>Catalogue</title></head>'
        f'<body style="{STYLE}"><h1>Catalogue</h1>{"".join(rows)}</body></html>'
    )


def picked_page(n):
    return (
        f'<!doctype html><html><head><title>Picked {n}</title></head>'
        f'<body style="{STYLE}"><h1>Picked item {n}</h1>'
        f'<p id="done">You opened catalogue item {n}.</p></body></html>'
    )


def slow_result():
    time.sleep(4.0)
    return f"""<!doctype html><html><head><title>Saved</title></head>
<body style="{STYLE}"><h1>Saved</h1><p id="done">Your changes were saved.</p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/never":
            # Hold the connection open so document.readyState never reaches complete.
            time.sleep(3600)
            return
        if parsed.path == "/slow-result":
            self._send(slow_result())
            return
        if parsed.path == "/favicon.ico":
            self._send("", status=204)
            return
        if parsed.path != "/trap":
            self._send("<!doctype html><title>Not found</title>", status=404)
            return

        mode = (params.get("mode") or ["dead"])[0]
        if mode == "dead":
            self._send(DEAD)
        elif mode == "ab":
            self._send(AB_B if (params.get("step") or ["1"])[0] == "2" else AB_A)
        elif mode == "loading":
            self._send(LOADING)
        elif mode == "form":
            self._send(FORM)
        elif mode == "results":
            self._send(results_page((params.get("q") or [""])[0], (params.get("sort") or [""])[0]))
        elif mode == "list":
            self._send(list_page(
                int((params.get("count") or ["40"])[0]),
                int((params.get("target") or ["30"])[0]),
                (params.get("target_label") or ["Dude Perfect - Best Trick Shots"])[0],
            ))
        elif mode == "picked":
            self._send(picked_page((params.get("n") or ["0"])[0]))
        elif mode == "slow":
            self._send(
                f"""<!doctype html><html><head><title>Confirm</title></head>
<body style="{STYLE}"><h1>Confirm</h1>
<form id="confirm" action="/slow-result"><button id="save" type="submit">Save changes</button></form>
</body></html>"""
            )
        else:
            self._send("<!doctype html><title>Unknown mode</title>", status=404)

    def log_message(self, *_args):
        pass


def serve(port):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"trap server on http://127.0.0.1:{args.port}/trap?mode=dead", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
