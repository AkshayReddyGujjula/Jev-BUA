"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import activate_tab, cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")


def private_snapshot(values, *, marker=False):
    """Keep private literals inside one CDP evaluation, off the returned page."""
    tail = "return state?.marker ?? null;" if marker else "return state;"
    return (
        "((privateValues) => { const state=" + READ_STATE + "; " + tail + " })"
        "(" + json.dumps(list(values), ensure_ascii=False) + ")"
    )

TAB_MODES = ("foreground", "background", "active")
INTERNAL_URL_PREFIXES = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")

# How long Browser construction waits for document.readyState === "complete" after a
# navigation. The original 15 s deadline made every page with a hung subresource (an
# analytics beacon, a stalled image, a long-poll) a 15 s hang BEFORE the loop's own
# anti-loop deadline had even started - measured at 20.5 s end-to-end. settle() and
# fresh() already handle a page that is still loading, so this only has to cover the
# common case.
NAV_TIMEOUT = float(os.environ.get("JEV_NAV_TIMEOUT", "4"))
# How long to wait for a tab opened by the last action to appear. 4 s was disproportionate
# to the 5 s no-progress deadline it sits inside.
FOLLOW_TAB_TIMEOUT = float(os.environ.get("JEV_FOLLOW_TAB_TIMEOUT", "1.5"))
# Screens one caller-directed scan may traverse before handing back to the model.
SCAN_MAX_SCREENS = int(os.environ.get("JEV_SCAN_MAX_SCREENS", "200"))
# Consecutive unproductive scrolls before a scan calls the list exhausted. One is not enough:
# a page that is still settling, or a lazy-load that has not landed yet, looks exactly like the
# end of a list for a single step. Measured - the first scan of a run reliably exited after two
# screens on a page that then yielded the target five screens later. Matches the agent's own
# barren-scroll guard so the two agree on what "exhausted" means.
SCAN_BARREN_LIMIT = int(os.environ.get("JEV_SCAN_BARREN_LIMIT", "3"))

# How long a single caller-directed scan may run before handing back to the model.
SCAN_MAX_S = float(os.environ.get("JEV_SCAN_MAX_SECONDS", "120"))
# A screenshot is a luxury, never worth a 5 s IPC wait inside a run.
SCREENSHOT_TIMEOUT = float(os.environ.get("JEV_SCREENSHOT_TIMEOUT", "3"))


def render_keepalive_enabled():
    return os.environ.get("JEV_RENDER_KEEPALIVE", "1").strip().lower() not in {"0", "off", "no", "false"}


def target_visible(page, target):
    """True when `target` (case-insensitive) already appears in a clickable label or the text."""
    needle = (target or "").strip().lower()
    if not needle:
        return False
    labels = " ".join((a.get("label") or "") for a in page.get("actions") or []).lower()
    return needle in labels or needle in (page.get("text") or "").lower()


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def _visible_page_target():
    """The user's currently visible Chrome tab as (targetId, sessionId), or None.

    Only the visible tab of a window reports document.visibilityState == "visible";
    a visible tab that also has focus wins when several windows are open.
    One probe session per candidate is detached again; only the chosen one is kept.
    """
    best = None
    for info in cdp("Target.getTargets")["targetInfos"]:
        if info.get("type") != "page" or info.get("url", "").startswith(INTERNAL_URL_PREFIXES):
            continue
        session = cdp("Target.attachToTarget", targetId=info["targetId"], flatten=True)["sessionId"]
        try:
            value = cdp(
                "Runtime.evaluate",
                session_id=session,
                expression="[document.visibilityState, document.hasFocus()]",
                returnByValue=True,
            )["result"].get("value")
        except Exception:
            value = None
        visible = bool(value and value[0] == "visible")
        if visible and value[1]:
            if best:
                cdp("Target.detachFromTarget", sessionId=best[1])
            return info["targetId"], session
        if visible and best is None:
            best = (info["targetId"], session)
            continue
        cdp("Target.detachFromTarget", sessionId=session)
    return best


class Browser:
    def __init__(self, url, *, follow_tabs=None):
        ensure_daemon()
        self.private_values = []
        self.follow_tabs = follow_tabs
        self.mode = os.environ.get("JEV_TAB_MODE", "foreground").strip().lower()
        if self.mode not in TAB_MODES:
            raise ValueError(f"JEV_TAB_MODE must be one of {TAB_MODES}, got {self.mode!r}")
        self.owns_target = self.mode != "active"
        if self.mode == "active":
            found = _visible_page_target()
            if not found:
                raise RuntimeError(
                    "No visible Chrome tab found for JEV_TAB_MODE=active; open a page or use foreground mode."
                )
            self.target, self.session = found
        else:
            background = self.mode == "background"
            self.target = cdp("Target.createTarget", url="about:blank", background=background)["targetId"]
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            if not background:
                activate_tab(self.target)
            if background:
                # Emulated metrics keep hidden snapshots consistent; visible tabs use the real window size.
                self.call(
                    "Emulation.setDeviceMetricsOverride",
                    width=1120, height=780, deviceScaleFactor=1, mobile=False,
                )
        self._known_targets = {t["targetId"] for t in self._page_targets()}
        self.owned_targets = [self.target] if self.owns_target else []
        # Keep rAF/menus rendering even when the tab is not the visible one.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.rendering = self.mode != "background"
        self._keep_rendering()
        if url:
            self.navigate(url)

    def navigate(self, url):
        """Point this tab at `url` and wait for the document, the same way __init__ does.

        Used by a warm session: the tab is reused, but every call starts from a freshly
        navigated document so no state leaks between verification runs.
        """
        if not url:
            return
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + NAV_TIMEOUT
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        self.last_url = url
        if not any(t["targetId"] == self.target for t in self._page_targets()):
            # Chrome 153 replaces the target on navigation: adopt the replacement before the first read.
            self.reattach()

    def history_index(self):
        """This tab's navigation-history index. Read-only CDP; no page script runs."""
        return int(self.call("Page.getNavigationHistory").get("currentIndex", 0))

    def call(self, method, **params):
        # CDP calls include navigation and Runtime.evaluate expressions that can
        # mutate the page. A lost reply cannot establish whether they ran.
        return cdp(method, session_id=self.session, **params)

    def _keep_rendering(self):
        """Make a background tab produce frames, so the page renders like a visible one.

        Measured 2026-09-23 on Chrome 153: focus emulation alone leaves a background tab of
        a real site (Google Flights) at 0-2 requestAnimationFrame callbacks per 500 ms, so a
        JavaScript-driven menu never finishes opening - its options stay at opacity 0, the
        snapshot correctly drops them as invisible, and the agent loops on a control whose
        choices it cannot see. Page.captureScreenshot never returns either. An active
        screencast makes Chrome produce frames for the target: 30-32 callbacks per 500 ms,
        the menu opens within 50 ms, and a screenshot returns in ~80 ms. Frames are never
        acknowledged, so Chrome sends about three tiny frames and then stops sending while
        rendering continues (verified over 20 s). JEV_RENDER_KEEPALIVE=0 turns it off.
        """
        if getattr(self, "mode", None) != "background" or not render_keepalive_enabled():
            return
        try:
            self.call("Page.startScreencast", format="jpeg", quality=1, maxWidth=32, maxHeight=32)
            self.rendering = True
        except Exception:
            # Rendering stays throttled; observation still works, screenshots are skipped.
            self.rendering = False

    @property
    def can_screenshot(self):
        """False for a background tab that is not rendering: its capture would never return."""
        return bool(getattr(self, "rendering", True))

    def screenshot(self, quality=60, timeout=None):
        if not self.can_screenshot:
            raise RuntimeError("This background tab is not rendering; no screenshot is available.")
        return cdp(
            "Page.captureScreenshot", session_id=self.session,
            _response_timeout=timeout or SCREENSHOT_TIMEOUT, format="jpeg", quality=quality,
        )["data"]

    @staticmethod
    def _session_lost(error):
        return "session with given id" in str(error).lower()

    def reattach(self):
        """Re-attach after a dropped CDP session, adopting the replacement target when
        Chrome re-creates it on navigation. Fails only when no usable page target exists.

        Adoption is deliberately narrow. The previous fallback was `real[-1]` - whichever
        page happened to be last in the target list - which in a shared browser silently
        moved an instance onto ANOTHER instance's tab, and the run then drove a page it did
        not own (observed as non-deterministic parallel checks: a worker ending on a URL no
        check ever visits, reached through history that was not its own). An instance now
        re-attaches only to a target it can attribute to itself, and returns False when the
        candidate set is ambiguous, so the caller sees an error instead of the wrong page.
        """
        pages = self._page_targets()
        ids = {t["targetId"] for t in pages}
        owned = {t["targetId"]: t for t in pages if t["targetId"] in set(self.owned_targets)}
        target = self.target if self.target in ids else None

        if target is None:
            known_url = getattr(self, "last_url", "")
            if len(owned) == 1:
                target = next(iter(owned))
            elif self.owns_target:
                # Chrome can replace our target on navigation, but an existing
                # same-URL tab belongs to somebody else. Only a *new* target
                # with that URL is a candidate, and ambiguity fails closed.
                matches = [
                    t for t in pages
                    if known_url and t.get("url") == known_url
                    and t["targetId"] not in self._known_targets
                ]
                if len(matches) != 1:
                    return False
                target = matches[0]["targetId"]
            elif not self.owns_target:
                # Active mode: the user's visible tab is ours to re-find.
                found = _visible_page_target()
                if not found:
                    return False
                target = found[0]

        if target != self.target:
            if self.owns_target and target not in self.owned_targets:
                self.owned_targets.append(target)
            self._known_targets.add(target)
            self.target = target
        self.session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        # A screencast belongs to the session it was started on.
        self._keep_rendering()
        return True

    def _browser_operation(self, request):
        try:
            return browser_operation(request)
        except RuntimeError as error:
            # A lost reply does not tell us whether an action already ran. Replaying
            # it could submit a form, buy something, or type twice. Observations are
            # safe to retry after reattaching; mutations must surface as uncertain.
            if request.get("operation") != "observe" or not self._session_lost(error) or not self.reattach():
                raise
            return browser_operation({**request, "session": self.session})

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    SCROLL_CONTENT = (
        "(() => {"
        "  const x = Math.floor(innerWidth / 2), y = Math.floor(innerHeight / 2);"
        "  const under = (document.elementsFromPoint ? document.elementsFromPoint(x, y) : []);"
        "  const target = under.find(e => e.scrollHeight > e.clientHeight + 40)"
        "    || document.scrollingElement;"
        "  if (!target) return null;"
        "  const before = target.scrollTop;"
        "  target.scrollTop = before + __DELTA__;"
        "  return target.scrollTop;"
        "})()"
    )

    def scroll_content(self, delta=600):
        """Scroll whatever element actually scrolls under the viewport centre.

        Some feeds (x.com) put the timeline in an inner scroller, so the document offers no
        scroll action and a collector has nothing to click. This is a generic browser
        operation - find the scrollable element under the cursor and move it - not a site
        plan. Returns the new offset, or None when nothing scrollable was found.
        """
        return self.evaluate(self.SCROLL_CONTENT.replace("__DELTA__", str(int(delta))))

    SETTLE_SIGNATURE = (
        "(document.readyState==='complete'?1:0)+':'+"
        "((document.body?document.body.innerText.length:0)+':'+"
        "document.querySelectorAll('a,button,input,select,textarea').length)"
    )

    # A rendering frame is ~16.7 ms. Two equal samples must span at least SETTLE_EVIDENCE
    # before the page counts as settled: a shorter window can straddle a pause mid-load
    # and return while the page is still mutating (a wasted observation). 50 ms is still
    # ~5x under the old flat 250 ms sleep, which made 255 ms the floor for EVERY read.
    SETTLE_INTERVAL = 0.02
    SETTLE_EVIDENCE = 0.05
    SETTLE_INTERVAL_MAX = 0.15
    SETTLE_BACKOFF = 1.9

    def settle(self, timeout=1.0, interval=None, deadline=None):
        """Wait until two consecutive DOM fingerprints match (page stopped rendering), capped.

        An already-idle page settles in roughly 50-100 ms; the previous flat 250 ms sleep
        made 255 ms the floor for every observation. A page that is genuinely still changing
        pays the wait, and the interval backs off so a slow loader is not polled at full rate.

        `deadline` is an absolute monotonic time the wait must not cross. Callers inside a
        run pass the anti-loop deadline, so an observation started near it cannot push the
        stop past the guarantee by a full settle window.

        What the readyState term does and does not buy: a HARD navigation flips readyState
        loading -> complete at most twice per document, which forces a re-sample and stops a
        half-loaded page being mistaken for a settled one. It does NOT detect a soft
        (pushState) route change, which leaves readyState complete on both sides - that case
        is caught by the freshness marker in fresh()/act(), which is the guard that actually
        protects an action. Signature equality here is an optimisation; fresh() is the safety
        property.

        Transient protocol errors are treated as "not yet settled" and bounded by the
        deadline rather than propagated: sampling more often widens the window in which a
        navigation tears down the execution context.
        """
        end = time.monotonic() + timeout
        if deadline is not None:
            end = min(end, deadline)
        wait = interval or self.SETTLE_INTERVAL
        previous = None
        stable_since = None
        while True:
            try:
                current = self.evaluate(self.SETTLE_SIGNATURE)
            except (StalePage, RuntimeError):
                current = None
            now = time.monotonic()
            if current is not None and current == previous:
                if stable_since is not None and (now - stable_since) >= self.SETTLE_EVIDENCE:
                    return
            else:
                stable_since = now
            previous = current
            if now >= end:
                return
            time.sleep(wait)
            wait = min(wait * self.SETTLE_BACKOFF, self.SETTLE_INTERVAL_MAX)

    def _page_targets(self):
        return [t for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"]

    def follow_new_tab(self, timeout=None):
        """Adopt a page target opened by the last action, if one appeared and looks like a real page."""
        if getattr(self, "follow_tabs", None) is False or os.environ.get("JEV_FOLLOW_TABS", "1") == "0":
            return False
        deadline = time.monotonic() + (timeout or FOLLOW_TAB_TIMEOUT)
        while True:
            newcomers = [t for t in self._page_targets() if t["targetId"] not in self._known_targets]
            real = [t for t in newcomers if not t.get("url", "").startswith(INTERNAL_URL_PREFIXES)]
            if real:
                target = real[0]["targetId"]
                self.session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
                self.target = target
                self._known_targets.add(target)
                if self.owns_target:
                    self.owned_targets.append(target)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def scan(self, action, target, page, max_screens=None, deadline=None, on_observe=None):
        """Scroll until `target` appears. Returns (page, screens, found).

        A caller-supplied target turns a scan into deterministic work: once the model has
        said "the target is not visible", scrolling further needs no new decision. Every
        screen is still OBSERVED, which is the whole reason this is safe - a batch of blind
        scrolls would jump past the target and the model would never see it.

        Stops on: the target appearing, the list offering no further scroll, several consecutive
        scrolls that reveal nothing new, the screen bound, or `deadline`.
        """
        needle = (target or "").strip().lower()
        seen = {(a.get("label") or "") for a in page.get("actions") or []}
        screens = 0
        barren = 0
        found = target_visible(page, needle)
        while not found and screens < (max_screens or SCAN_MAX_SCREENS):
            current_deadline = deadline() if callable(deadline) else deadline
            if current_deadline is not None and time.monotonic() >= current_deadline:
                break
            current = next((a for a in page["actions"] if a["id"] == action["id"]), None)
            if current is None:
                break                      # no scroll offered: end of the list
            self.act(current, page)
            page = (
                self.observe(screenshot=False, deadline=current_deadline)
                if current_deadline is not None else self.observe(screenshot=False)
            )
            screens += 1
            if on_observe is not None:
                on_observe(page)
            if target_visible(page, needle):
                found = True
                break
            labels = {(a.get("label") or "") for a in page.get("actions") or []}
            if labels - seen:
                barren = 0
            else:
                barren += 1
                if barren >= SCAN_BARREN_LIMIT:
                    break                  # nothing new for several steps: genuinely exhausted
            seen |= labels
        return page, screens, found

    def observe(self, screenshot=True, deadline=None):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        self.settle(deadline=deadline)
        for attempt in range(10):
            try:
                page = self._browser_operation(
                    {"operation": "observe", "session": self.session,
                     "screenshot": screenshot,
                     "private_values": getattr(self, "private_values", [])}
                )
                self.last_url = page.get("url", getattr(self, "last_url", ""))
                return page
            except StalePage:
                if attempt == 9 or (
                    deadline is not None and attempt >= 2 and time.monotonic() > deadline
                ):
                    # Past the run's deadline: a short retry budget, so the stop is not
                    # waiting on a long loop of reads.
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def _fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(private_snapshot(getattr(self, "private_values", []), marker=True)) == page["marker"]

    def fresh(self, page, action=None):
        try:
            return self._fresh(page, action)
        except RuntimeError as error:
            if not self._session_lost(error) or not self.reattach():
                raise
            return self._fresh(page, action)

    def value_present(self, node, text):
        """Read only a boolean from Chrome; never return the private DOM value."""
        if type(node) is not int or not isinstance(text, str):
            return False
        payload = json.dumps({"node": node, "text": text}, ensure_ascii=False)
        return bool(self.evaluate(
            "(query => { const e=window.__jevFast?.nodes.get(query.node); "
            "return !!e?.isConnected && String(e.value ?? e.innerText).trim() === query.text.trim(); "
            "})(" + payload + ")"
        ))

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = self._browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] not in {"wait", "back", "key", "click_at"} else None
        return result

    def close(self):
        targets, self.owned_targets = self.owned_targets, []
        if os.environ.get("JEV_KEEP_TAB") != "1":
            for target in targets:
                try:
                    cdp("Target.closeTarget", targetId=target)
                except Exception:
                    pass
        self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "back":
            history = call("Page.getNavigationHistory")
            index = history.get("currentIndex", 0)
            entries = history.get("entries") or []
            if index <= 0:
                raise RuntimeError("No browser history to go back to")
            call("Page.navigateToHistoryEntry", entryId=entries[index - 1]["id"])
        elif kind == "scroll":
            viewport = evaluate("({x: Math.floor(innerWidth / 2), y: Math.floor(innerHeight / 2)})") or {}
            call(
                "Input.dispatchMouseEvent",
                type="mouseWheel",
                x=viewport.get("x", 550),
                y=viewport.get("y", 650),
                deltaX=0,
                deltaY=action["delta"],
            )
        elif kind == "key":
            event = {
                "key": action["key"],
                "code": action.get("code", ""),
                "modifiers": 8 if action.get("shift") else 0,
                "windowsVirtualKeyCode": action.get("vk", 0),
                "nativeVirtualKeyCode": action.get("vk", 0),
            }
            if len(action["key"]) == 1:
                event["text"] = action["key"]
            call("Input.dispatchKeyEvent", type="keyDown", **event)
            call("Input.dispatchKeyEvent", type="keyUp", **event)
        elif kind == "click_at":
            for event in ("mousePressed", "mouseReleased"):
                call(
                    "Input.dispatchMouseEvent",
                    type=event, x=int(action["x"]), y=int(action["y"]), button="left", clickCount=1,
                )
        elif kind == "seek":
            payload = json.dumps({"node": action.get("node"), "value": str(request.get("text"))})
            target = evaluate(
                "(action => {"
                "  const e=window.__jevFast?.nodes.get(action.node);"
                "  if (!e?.isConnected) return null;"
                "  const r=e.getBoundingClientRect();"
                "  if (!r.width || !r.height) return null;"
                "  const min=Number(e.getAttribute('aria-valuemin') ?? 0);"
                "  const max=Number(e.getAttribute('aria-valuemax') ?? 100);"
                "  const v=Number(action.value);"
                "  if (!Number.isFinite(v)) return null;"
                "  const ratio=max>min ? (Math.min(max,Math.max(min,v))-min)/(max-min) : 0.5;"
                "  const x=r.x+r.width*ratio, y=r.y+r.height/2;"
                "  if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;"
                "  return {x,y};"
                "})(" + payload + ")"
            )
            if target is None:
                raise RuntimeError("Seek target is not actionable")
            for event in ("mousePressed", "mouseReleased"):
                call("Input.dispatchMouseEvent", type=event, x=target["x"], y=target["y"], button="left", clickCount=1)
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(private_snapshot(request.get("private_values") or []))
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
