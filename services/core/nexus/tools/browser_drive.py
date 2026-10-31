"""Driving a real browser (task C8; spec §4 N6/O9, §37, §57, §78).

``browser.open`` hands a URL to the user's browser and then knows nothing.
``net.fetch`` downloads a page but only ever sees the HTML the server sent. This
module is the third thing, and the only one that can answer "what is actually on
the page" and "did clicking that work": a browser NEXUS starts itself and speaks
the DevTools Protocol to.

**Every URL goes through both guards.** ``policy.url_refusal`` catches the shapes
policy knows about, and ``browser._assert_public`` resolves the host and refuses
private, loopback and link-local addresses. Both are applied here, before
navigation, for the same reason they are applied in ``net.fetch``: a tool that
fetches a URL is how a request to a cloud metadata endpoint gets made. The
private import from the sibling module is deliberate -- the alternative is a
second copy of a security-critical function, which is how the two copies drift
and one of them stops refusing.

**Verification reads the page back.** Nothing here reports what it intended to
do. ``browser.navigate`` re-reads ``location.href``, ``document.title`` and
``document.readyState``; ``browser.type`` re-reads the field's value;
``browser.screenshot`` reads the PNG's own header for its dimensions. A tool
whose only evidence is its own return value is a tool that cannot fail (§78).

**What this cannot do, stated plainly.** A selector only sees the top-level
document: content inside an iframe is invisible to it, and so is anything inside
a closed shadow root. A page that renders entirely on canvas -- a remote desktop,
a PDF viewer in an embed -- has no elements to query at all; ``browser.screenshot``
is the tool for those, and it is why that tool exists.
"""

from __future__ import annotations

import base64
import json
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ..browser.driver import BrowserUnavailable, ChromeCommandError, session
from ..config import settings
from ..permissions import policy
from ..core.ws_client import WebSocketError
from ..logging_setup import get_logger
from .browser import BlockedAddress, _assert_public
from .registry import ToolResult, tool

log = get_logger("tools.browser_drive")

#: Above the sum of the internal waits (25 s to launch + 25 s to navigate + the
#: read-back), for the same reason ``terminal.run`` does it: if the runtime's
#: outer timeout fired first it would report a timeout while this function was
#: still holding a live browser.
BROWSER_TOOL_TIMEOUT_S = 120.0
QUICK_TOOL_TIMEOUT_S = 60.0

MAX_READ_CHARS = 60_000
MAX_QUERY_MATCHES = 100

#: The DevTools Protocol reports these as errors, but they are the *expected*
#: answer when a selector matches nothing. Folding them into "the tool broke"
#: would make "there is no such button on this page" indistinguishable from
#: "NEXUS is misconfigured".
_MISSING_ELEMENT = object()


def _url_refusal(url: str) -> str | None:
    """Both guards, in the same order ``net.fetch`` applies them."""
    refusal = policy.url_refusal(url)
    if refusal is not None:
        return f"{refusal.reason} {refusal.detail}".strip()
    try:
        _assert_public(urllib.parse.urlparse(url).hostname or "")
    except BlockedAddress as exc:
        return str(exc)
    return None


def _js(value: Any) -> str:
    """A JavaScript literal for a Python value.

    ``json.dumps`` output is a valid JS expression for every JSON type, and it
    escapes the quotes, backslashes and newlines that would otherwise let a
    selector or a search string terminate the string it is embedded in.
    """
    return json.dumps(value)


def _browser() -> Any:
    """The session, started if needed. Raises BrowserUnavailable with the reason."""
    live = session()
    if not live.running:
        live.start()
    return live


def _fail(exc: Exception) -> ToolResult:
    if isinstance(exc, ChromeCommandError):
        return ToolResult(ok=False, error=str(exc), summary="The browser refused the command")
    return ToolResult(ok=False, error=str(exc), summary="The browser could not do that")


# ------------------------------------------------------------------ JS sources


_TEXT_JS = """
(() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS','IFRAME']);
  const BREAK = new Set(['P','DIV','BR','LI','TR','H1','H2','H3','H4','H5','H6',
                         'SECTION','ARTICLE','HEADER','FOOTER','BLOCKQUOTE','PRE','TD','TH']);
  const parts = [];
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const text = child.textContent.replace(/\\s+/g, ' ');
        if (text.trim()) parts.push(text);
      } else if (child.nodeType === 1 && !SKIP.has(child.tagName)) {
        walk(child);
        if (BREAK.has(child.tagName)) parts.push('\\n');
      }
    }
  };
  walk(document.body || document.documentElement);
  return parts.join('').replace(/[ \\t]+/g, ' ')
              .replace(/\\n\\s*\\n\\s*\\n+/g, '\\n\\n')
              .split('\\n').map(l => l.trim()).join('\\n').trim();
})()
"""

_LINKS_JS = """
(() => Array.from(document.querySelectorAll('a[href]')).slice(0, LIMIT).map(a => ({
  text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
  href: a.href
})).filter(l => l.href && l.href.startsWith('http')))()
"""

_TABLES_JS = """
(() => Array.from(document.querySelectorAll('table')).slice(0, LIMIT).map(t => ({
  headers: Array.from(t.querySelectorAll('thead th, tr:first-child th')).map(
    h => (h.innerText || '').replace(/\\s+/g, ' ').trim()),
  rows: Array.from(t.querySelectorAll('tbody tr, tr')).slice(0, 50).map(
    tr => Array.from(tr.querySelectorAll('td, th')).map(
      td => (td.innerText || '').replace(/\\s+/g, ' ').trim()))
})))()
"""

_PAGE_JS = """
(() => ({
  url: location.href,
  title: document.title,
  ready_state: document.readyState,
  links: document.querySelectorAll('a[href]').length,
  forms: document.forms.length,
  text_length: (document.body ? document.body.innerText || '' : '').length,
  elements: document.querySelectorAll('*').length
}))()
"""

_FINGERPRINT_JS = """
(() => {
  const body = document.body ? (document.body.innerText || '') : '';
  let hash = 0;
  const sample = body.slice(0, 4000);
  for (let i = 0; i < sample.length; i++) {
    hash = ((hash << 5) - hash + sample.charCodeAt(i)) | 0;
  }
  return location.href + '|' + document.title + '|' +
         document.querySelectorAll('*').length + '|' + hash;
})()
"""


def _resolver_js(selector: str | None, text: str | None) -> str:
    """A JS expression that evaluates to one element, or null.

    Two ways to name a target, and they are genuinely different. A selector is
    what the model uses when it can see the markup; ``text`` is what it uses when
    it can only see what a person would see -- and a person clicks the button
    that says "Sign in", not ``div.MuiButton-root:nth-child(3)``.
    """
    if selector:
        return f"document.querySelector({_js(selector)})"
    return f"""(function () {{
      const want = {_js(text or '')};
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const nodes = Array.from(document.querySelectorAll(
        'a,button,input[type=submit],input[type=button],[role=button],[onclick],summary,label'));
      return nodes.find(n => norm(n.innerText || n.value) === want)
          || nodes.find(n => norm(n.innerText || n.value).toLowerCase() === want.toLowerCase())
          || nodes.find(n => norm(n.innerText || n.value).toLowerCase().includes(want.toLowerCase()))
          || null;
    }})()"""


def _element_info_js(resolver: str) -> str:
    return f"""(function () {{
      const el = {resolver};
      if (!el) return null;
      el.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = el.getBoundingClientRect();
      return {{
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
        id: el.id || null,
        name: el.getAttribute('name'),
        type: el.getAttribute('type'),
        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        width: r.width,
        height: r.height
      }};
    }})()"""


# ------------------------------------------------------------------- navigate


def _verify_navigate(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Re-read the live page and report where it actually is.

    The point is the ``final_url``: a navigation that redirects (or that a site
    rewrites client-side) means the address asked for is not the address the
    model is now reading. Confirming that against the browser rather than against
    the tool's own report is the difference between evidence and an echo.
    """
    if not result.ok or not result.data:
        return False, result.error or "The navigation did not complete."
    try:
        page = session().evaluate(_PAGE_JS)
    except (BrowserUnavailable, WebSocketError, ChromeCommandError) as exc:
        return False, f"The page could not be read back to confirm the navigation: {exc}"
    if not isinstance(page, dict):
        return False, "The page did not report its own state."
    if page.get("ready_state") != "complete":
        return False, f"The page is still loading (readyState: {page.get('ready_state')})."
    returned = result.data.get("final_url")
    if returned and page.get("url") != returned:
        return False, (
            f"The page reports it is at {page.get('url')}, not {returned} as the "
            "command reported."
        )
    return True, (
        f"Confirmed by reading the page: {page.get('url')} is loaded, titled "
        f"'{page.get('title')}', with {page.get('elements')} elements."
    )


@tool(
    "browser.navigate",
    title="Open a page in the browser NEXUS drives",
    description=(
        "Open a URL in a browser NEXUS controls and wait for it to load, then report "
        "the final address, the title and what is on the page. Use this instead of "
        "opening the page in your own browser when NEXUS needs to read or interact "
        "with what is on it. This browser has no logins and no cookies of yours."
    ),
    capability="browser.drive",
    idempotent=True,
    timeout_s=BROWSER_TOOL_TIMEOUT_S,
    verifier=_verify_navigate,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    voice_start="Opening {url} in the browser",
    voice_timeout="The browser is not responding. Trying again.",
    voice_error="Could not open the page in the browser.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 4, "description": "Full https:// address."},
            "timeout_seconds": {
                "type": "integer", "minimum": 5, "maximum": 60, "default": 25,
                "description": "How long to wait for the page to finish loading.",
            },
        },
        "required": ["url"],
    },
)
def browser_navigate(params: dict[str, Any]) -> ToolResult:
    url = str(params["url"]).strip()
    if "://" not in url:
        url = f"https://{url}"

    refusal = _url_refusal(url)
    if refusal is not None:
        return ToolResult(ok=False, error=refusal, summary="That address is refused")

    wait = float(params.get("timeout_seconds", 25))
    try:
        live = _browser()
        outcome = live.goto(url, timeout=wait)
        page = live.evaluate(_PAGE_JS)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    page = page if isinstance(page, dict) else {}
    final_url = page.get("url") or url
    redirected = final_url.rstrip("/") != url.rstrip("/")

    return ToolResult(
        ok=True,
        data={
            "requested_url": url,
            "final_url": final_url,
            "redirected": redirected,
            "title": page.get("title", ""),
            "ready_state": page.get("ready_state") or outcome.get("ready_state"),
            "completed": outcome.get("completed", False),
            "links": page.get("links", 0),
            "forms": page.get("forms", 0),
            "text_length": page.get("text_length", 0),
            "browser": live.version,
        },
        summary=(
            f"Opened {final_url}"
            + (f" (redirected from {url})" if redirected else "")
            + (f" -- {page.get('title')}" if page.get("title") else "")
        ),
    )


# ----------------------------------------------------------------------- read


def _verify_read(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.ok or not result.data:
        return False, result.error or "Nothing was read."
    if result.data.get("empty"):
        return False, (
            "The page loaded but there was no readable text in it. A page that renders "
            "only on a canvas has no text to extract; use browser.screenshot for those."
        )
    characters = int(result.data.get("characters", 0))
    return True, (
        f"Read {characters:,} characters from the live page at "
        f"{result.data.get('url', 'the current address')}."
    )


@tool(
    "browser.read",
    title="Read what is on the page",
    description=(
        "Read the text, links or tables of the page the browser is currently showing. "
        "Unlike fetch_page this reads what the browser actually rendered, so it sees "
        "pages built by JavaScript. Iframes and canvas-only content are invisible to "
        "it -- use browser.screenshot for those."
    ),
    capability="browser.drive",
    read_only=True,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_read,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["text", "links", "tables"],
                "default": "text",
            },
            "max_characters": {
                "type": "integer", "minimum": 500, "maximum": MAX_READ_CHARS, "default": 20_000,
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": MAX_QUERY_MATCHES, "default": 50,
                "description": "How many links or tables to return.",
            },
        },
    },
)
def browser_read(params: dict[str, Any]) -> ToolResult:
    mode = str(params.get("mode", "text"))
    limit = int(params.get("limit", 50))
    cap = int(params.get("max_characters", 20_000))

    if mode == "links":
        expression = _LINKS_JS.replace("LIMIT", str(limit))
    elif mode == "tables":
        expression = _TABLES_JS.replace("LIMIT", str(limit))
    else:
        expression = _TEXT_JS

    try:
        live = _browser()
        value = live.evaluate(expression, timeout=30.0)
        page = live.evaluate(_PAGE_JS)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    page = page if isinstance(page, dict) else {}
    data: dict[str, Any] = {"url": page.get("url", ""), "title": page.get("title", ""), "mode": mode}

    if mode == "text":
        text = value if isinstance(value, str) else ""
        clipped = text[:cap]
        data.update({
            "text": clipped,
            "characters": len(clipped),
            "characters_available": len(text),
            "truncated": len(clipped) < len(text),
            "empty": not text.strip(),
        })
    else:
        items = value if isinstance(value, list) else []
        data.update({
            "items": items,
            "count": len(items),
            "empty": not items,
        })

    return ToolResult(
        ok=True,
        data=data,
        summary=(
            f"Read {data.get('characters', 0):,} characters from {page.get('url', 'the page')}"
            if mode == "text"
            else f"Found {data.get('count', 0)} {mode} on {page.get('url', 'the page')}"
        ),
    )


# ---------------------------------------------------------------------- query


def _verify_query(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.ok or not result.data:
        return False, result.error or "Nothing was found."
    count = int(result.data.get("count", 0))
    selector = result.data.get("selector", "")
    if count == 0:
        return True, (
            f"No element on the page matches {selector!r}. This is a real answer, not a "
            "failure -- the page loaded and was searched."
        )
    return True, f"{count} element(s) match {selector!r} on the loaded page."


@tool(
    "browser.query",
    title="Find elements on the page",
    description=(
        "Find elements matching a CSS selector and report what they are: their text, "
        "ids, names, types and whether they are visible. Use this to decide what to "
        "click or type into. Only the top-level document is searched -- elements "
        "inside an iframe will not be found."
    ),
    capability="browser.drive",
    read_only=True,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_query,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_MATCHES, "default": 20},
        },
        "required": ["selector"],
    },
)
def browser_query(params: dict[str, Any]) -> ToolResult:
    selector = str(params["selector"])
    limit = int(params.get("limit", 20))

    expression = f"""(function () {{
      const nodes = Array.from(document.querySelectorAll({_js(selector)}));
      return {{
        count: nodes.length,
        matches: nodes.slice(0, {limit}).map((el, index) => ({{
          index: index,
          tag: el.tagName.toLowerCase(),
          text: (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 300),
          id: el.id || null,
          name: el.getAttribute('name'),
          type: el.getAttribute('type'),
          href: el.getAttribute('href'),
          value: (typeof el.value === 'string' ? el.value : null),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        }}))
      }};
    }})()"""

    try:
        live = _browser()
        value = live.evaluate(expression, timeout=30.0)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    value = value if isinstance(value, dict) else {}
    count = int(value.get("count", 0))
    matches = value.get("matches") or []

    return ToolResult(
        ok=True,
        data={"selector": selector, "count": count, "matches": matches},
        summary=(
            f"{count} element(s) match {selector}"
            if count
            else f"Nothing on the page matches {selector}"
        ),
    )


# ---------------------------------------------------------------------- click


def _verify_click(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Confirm the page is still alive and report whether it actually changed.

    A click that leaves no trace is a real outcome, not necessarily a failure --
    clicking a label toggles a checkbox that this fingerprint does not see. So the
    wording reports what was observed and does not claim more.
    """
    if not result.ok or not result.data:
        return False, result.error or "The click did not happen."
    try:
        live = session()
        now = live.evaluate(_FINGERPRINT_JS)
    except (BrowserUnavailable, WebSocketError, ChromeCommandError) as exc:
        # A navigation into a new process is a *likely* outcome of a click, so a
        # dead connection here is not proof the click failed.
        if result.data.get("changed"):
            return True, (
                "The page changed after the click and the connection was replaced, which "
                "is what a navigation looks like. The click took effect."
            )
        return False, f"The page could not be read back after the click: {exc}"

    after = result.data.get("fingerprint_after")
    if now != after:
        return True, (
            "The page has changed again since the click was reported, so it is live and "
            f"responsive (now at {str(now).split('|')[0]})."
        )
    if result.data.get("changed"):
        return True, (
            f"The page state changed when the click was dispatched: "
            f"{result.data.get('change_note')}"
        )
    return True, (
        "The page is still responsive. Nothing about its address, title or text changed, "
        "which is normal for a click that only affects a control (a checkbox, a menu) -- "
        "NEXUS cannot confirm it did what you intended, only that the page is alive."
    )


@tool(
    "browser.click",
    title="Click something on the page",
    description=(
        "Click an element, named either by a CSS selector or by its visible text. "
        "NEXUS moves the pointer to the middle of the element and presses, the same "
        "way a person would, so pages that react to real mouse events work. "
        "This can submit forms and press buttons that spend money or delete things."
    ),
    capability="browser.drive",
    idempotent=False,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_click,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "maxLength": 500},
            "text": {
                "type": "string",
                "maxLength": 200,
                "description": "The visible text of the thing to click, e.g. 'Sign in'.",
            },
        },
    },
)
def browser_click(params: dict[str, Any]) -> ToolResult:
    selector = params.get("selector")
    text = params.get("text")
    if not selector and not text:
        return ToolResult(
            ok=False,
            error="Give either a selector or the visible text of the element to click.",
        )

    resolver = _resolver_js(str(selector) if selector else None, str(text) if text else None)

    try:
        live = _browser()
        before = live.evaluate(_FINGERPRINT_JS)
        info = live.evaluate(_element_info_js(resolver), timeout=20.0)

        if info is None:
            return ToolResult(
                ok=False,
                data={"selector": selector, "text": text},
                error=(
                    f"Nothing on the page at {before.split('|')[0] if isinstance(before, str) else 'this address'} "
                    f"matches {'selector ' + str(selector) if selector else 'the text ' + repr(text)}. "
                    "Use browser.query or browser.read to see what is actually there."
                ),
                summary="No such element",
            )

        if not info.get("visible"):
            return ToolResult(
                ok=False,
                data={"selector": selector, "text": text, "element": info},
                error=(
                    f"The element <{info.get('tag')}> is in the page but has no size, so it "
                    "cannot be clicked. It is probably hidden until something else happens."
                ),
                summary="Element is not visible",
            )

        x, y = float(info["x"]), float(info["y"])
        common = {"x": x, "y": y, "button": "left", "clickCount": 1, "pointerType": "mouse"}
        # A real click is a press and a release at the same coordinates, with the
        # buttons bitmask set only while the button is down. A page listening for
        # mousedown/mouseup sees the same sequence a person produces.
        live.command("Input.dispatchMouseEvent", {**common, "type": "mouseMoved", "buttons": 0})
        live.command("Input.dispatchMouseEvent", {**common, "type": "mousePressed", "buttons": 1})
        time.sleep(0.05)
        live.command("Input.dispatchMouseEvent", {**common, "type": "mouseReleased", "buttons": 0})

        # Give the page a moment to react before fingerprinting it again.
        time.sleep(0.4)
        after = live.evaluate(_FINGERPRINT_JS)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    changed = before != after
    note = ""
    if changed:
        before_url = str(before).split("|")[0]
        after_url = str(after).split("|")[0]
        if before_url != after_url:
            note = f"it navigated from {before_url} to {after_url}"
        else:
            before_title = str(before).split("|")[1]
            after_title = str(after).split("|")[1]
            note = (
                f"the title changed from {before_title!r} to {after_title!r}"
                if before_title != after_title
                else "the page content changed"
            )

    return ToolResult(
        ok=True,
        data={
            "selector": selector,
            "text": text,
            "element": info,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "changed": changed,
            "change_note": note,
            "url_after": str(after).split("|")[0],
        },
        summary=(
            f"Clicked <{info.get('tag')}> at ({x:.0f}, {y:.0f})"
            + (f" -- {note}" if changed else " (the page did not visibly change)")
        ),
    )


# ----------------------------------------------------------------------- type


def _verify_type(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.ok or not result.data:
        return False, result.error or "Nothing was typed."
    expected = result.data.get("text", "")
    try:
        live = session()
        actual = live.evaluate(
            f"(function () {{ const el = {result.data.get('resolver_js', 'null')}; "
            "return el ? String(el.value ?? '') : null; })()",
            timeout=15.0,
        )
    except (BrowserUnavailable, WebSocketError, ChromeCommandError) as exc:
        return False, f"The field could not be read back to confirm the typing: {exc}"

    if actual is None:
        return True, (
            "The text was sent to the page, but the element could not be re-read, so "
            "NEXUS cannot confirm the field holds it."
        )
    if actual != expected:
        return False, (
            f"The field now holds {actual!r}, not {expected!r} as intended. The page may "
            "have reformatted or rejected the input."
        )
    return True, f"Confirmed by reading the field back: it now holds {len(actual)} characters."


@tool(
    "browser.type",
    title="Type into a field on the page",
    description=(
        "Put text into an input, textarea or other editable element, optionally "
        "pressing Enter afterwards. The value is read back to confirm it landed. "
        "Do not type passwords or payment details into a page you did not open."
    ),
    capability="browser.drive",
    idempotent=False,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_type,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "maxLength": 500},
            "text": {"type": "string", "maxLength": 4000},
            "submit": {
                "type": "boolean", "default": False,
                "description": "Press Enter after typing.",
            },
            "clear_first": {"type": "boolean", "default": True},
        },
        "required": ["selector", "text"],
    },
)
def browser_type(params: dict[str, Any]) -> ToolResult:
    selector = str(params["selector"])
    text = str(params["text"])
    submit = bool(params.get("submit", False))
    clear_first = bool(params.get("clear_first", True))
    resolver = f"document.querySelector({_js(selector)})"

    try:
        live = _browser()
        info = live.evaluate(_element_info_js(resolver), timeout=20.0)
        if info is None:
            return ToolResult(
                ok=False,
                data={"selector": selector},
                error=f"No element on the page matches {selector!r}.",
                summary="No such field",
            )

        # Focus through the page rather than by clicking: clicking first would
        # move the pointer and could trigger a page's own hover or click handler.
        focused = live.evaluate(
            f"(function () {{ const el = {resolver}; if (!el) return false; el.focus(); "
            "if (typeof el.select === 'function') el.select(); return true; })()",
            timeout=10.0,
        )
        if not focused:
            return ToolResult(
                ok=False,
                data={"selector": selector},
                error=f"The element {selector!r} could not be focused, so it cannot be typed into.",
                summary="Could not focus the field",
            )

        if clear_first:
            live.evaluate(
                f"(function () {{ const el = {resolver}; if (el) el.value = ''; return true; }})()",
                timeout=10.0,
            )

        used = "insertText"
        live.command("Input.insertText", {"text": text}, timeout=20.0)
        landed = live.evaluate(
            f"(function () {{ const el = {resolver}; return el ? String(el.value ?? '') : null; }})()",
            timeout=10.0,
        )

        if landed != text:
            # ``insertText`` produces a real input event but no keystrokes, and a
            # few frameworks only listen for keys. The fallback writes through the
            # prototype's own setter and then dispatches the events those
            # frameworks listen for -- the standard way to set a value a
            # framework will notice, rather than assigning ``.value`` and having
            # React's own state overwrite it on the next render.
            used = "native-setter"
            live.evaluate(
                f"""(function () {{
                  const el = {resolver};
                  if (!el) return false;
                  const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                  if (setter && setter.set) setter.set.call(el, {_js(text)});
                  else el.value = {_js(text)};
                  el.dispatchEvent(new Event('input', {{bubbles: true}}));
                  el.dispatchEvent(new Event('change', {{bubbles: true}}));
                  return true;
                }})()""",
                timeout=10.0,
            )

        if submit:
            for kind in ("keyDown", "char", "keyUp"):
                live.command(
                    "Input.dispatchKeyEvent",
                    {
                        "type": kind,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                        "text": "\r" if kind == "char" else "",
                        "unmodifiedText": "\r" if kind == "char" else "",
                    },
                    timeout=15.0,
                )
            time.sleep(0.5)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    final = live.evaluate(_PAGE_JS)
    return ToolResult(
        ok=True,
        data={
            "selector": selector,
            "text": text,
            "submitted": submit,
            "method": used,
            "element": info,
            "resolver_js": resolver,
            "url_after": (final or {}).get("url", "") if isinstance(final, dict) else "",
        },
        summary=(
            f"Typed {len(text)} character(s) into {selector}"
            + (" and pressed Enter" if submit else "")
            + ("" if used == "insertText" else " (via the framework-compatible path)")
        ),
    )


# ----------------------------------------------------------------------- eval


def _verify_eval(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.ok or not result.data:
        return False, result.error or "Nothing was evaluated."
    kind = result.data.get("result_type")
    return True, (
        f"The page evaluated the expression and returned a {kind}"
        + (" (empty)" if result.data.get("result") is None else "")
        + "."
    )


@tool(
    "browser.eval",
    title="Run JavaScript in the page",
    description=(
        "Run a JavaScript expression inside the page and return its value. This is the "
        "escape hatch for pages the other browser tools cannot read -- anything you can "
        "do from a page's console, this does. It runs with the page's own privileges, "
        "so it can act as you on that site."
    ),
    capability="browser.drive",
    idempotent=False,
    read_only=False,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_eval,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "await_promise": {
                "type": "boolean", "default": False,
                "description": "Wait for the expression to resolve if it is a Promise.",
            },
        },
        "required": ["expression"],
    },
)
def browser_eval(params: dict[str, Any]) -> ToolResult:
    expression = str(params["expression"])
    await_promise = bool(params.get("await_promise", False))

    try:
        live = _browser()
        value = live.evaluate(expression, await_promise=await_promise, timeout=40.0)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    serialised = None
    truncated = False
    try:
        encoded = json.dumps(value, default=repr)
        truncated = len(encoded) > 40_000
        serialised = encoded[:40_000]
    except (TypeError, ValueError):
        serialised = repr(value)[:40_000]
        truncated = True

    return ToolResult(
        ok=True,
        data={
            "expression": expression,
            "result": value,
            "result_json": serialised,
            "result_type": type(value).__name__,
            "truncated": truncated,
        },
        summary=f"Evaluated in the page; the result is a {type(value).__name__}",
    )


# ----------------------------------------------------------------- screenshot


def _png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height straight out of the PNG's IHDR chunk."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _verify_screenshot(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Check the file on disk, not the buffer we think we wrote."""
    if not result.ok or not result.data:
        return False, result.error or "No screenshot was taken."
    path = Path(str(result.data.get("path", "")))
    if not path.exists():
        return False, f"{path} does not exist after the screenshot."
    raw = path.read_bytes()
    size = _png_size(raw)
    if size is None:
        return False, f"{path} is not a PNG image (its header is wrong for {len(raw)} bytes)."
    width, height = size
    if width == 0 or height == 0:
        return False, f"{path} is a PNG with no pixels ({width}x{height})."
    return True, f"Confirmed on disk: {path.name} is a {width}x{height} PNG, {len(raw):,} bytes."


@tool(
    "browser.screenshot",
    title="Screenshot the page",
    description=(
        "Capture an image of the page the browser is showing, saved to disk. Use this "
        "when the content cannot be read as text -- a canvas, a chart, a map, a PDF "
        "viewer. The file can then be looked at with the vision tools."
    ),
    capability="browser.drive",
    read_only=True,
    timeout_s=BROWSER_TOOL_TIMEOUT_S,
    verifier=_verify_screenshot,
    requires=("google-chrome|google-chrome-stable|chromium|chromium-browser|brave-browser",),
    parameters={
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean", "default": False,
                "description": "Capture the entire scrollable page, not just the window.",
            },
            "path": {"type": "string", "description": "Where to save it. Defaults to the cache."},
        },
    },
)
def browser_screenshot(params: dict[str, Any]) -> ToolResult:
    full_page = bool(params.get("full_page", False))
    try:
        live = _browser()
        result = live.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            timeout=40.0,
        )
        page = live.evaluate(_PAGE_JS)
    except (BrowserUnavailable, ChromeCommandError, WebSocketError) as exc:
        return _fail(exc)

    encoded = result.get("data")
    if not encoded:
        return ToolResult(ok=False, error="Chrome returned no image data.")

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        return ToolResult(ok=False, error=f"Chrome's image data could not be read: {exc}")

    if params.get("path"):
        target = Path(str(params["path"])).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        directory = settings().screenshot_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"nexus-page-{time.strftime('%Y%m%d-%H%M%S')}.png"

    try:
        target.write_bytes(raw)
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not save the screenshot to {target}: {exc}")

    size = _png_size(raw)
    page = page if isinstance(page, dict) else {}
    return ToolResult(
        ok=True,
        data={
            "path": str(target),
            "bytes": len(raw),
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "url": page.get("url", ""),
            "title": page.get("title", ""),
            "full_page": full_page,
        },
        summary=f"Saved a screenshot of {page.get('url', 'the page')} to {target.name}",
    )


# --------------------------------------------------------------------- close


def _verify_close(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    if not result.ok or not result.data:
        return False, result.error or "The browser was not closed."
    live = session()
    if live.running:
        return False, f"The browser is still running (pid {live.pid})."
    return True, "Confirmed: no browser process of NEXUS's is left running."


@tool(
    "browser.close",
    title="Close the browser NEXUS drives",
    description=(
        "Close the browser NEXUS opened for automation and end its session. Use this "
        "when a browsing task is finished, so a window is not left open on the "
        "desktop. NEXUS's browser closes on its own after five idle minutes anyway."
    ),
    capability="browser.drive",
    idempotent=True,
    read_only=False,
    timeout_s=QUICK_TOOL_TIMEOUT_S,
    verifier=_verify_close,
    parameters={"type": "object", "properties": {}},
)
def browser_close(params: dict[str, Any]) -> ToolResult:
    live = session()
    was_running = live.running
    outcome = live.close()
    return ToolResult(
        ok=True,
        data={"was_running": was_running, **outcome},
        summary=(
            f"Closed the browser ({outcome.get('detail')})"
            if was_running
            else "The browser was not running, so there was nothing to close"
        ),
    )
