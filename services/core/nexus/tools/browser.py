"""Browser and network tools (task D6; spec §37, §57, §78).

Three separate powers, each with its own capability:

* ``browser.open`` hands a URL to the user's own browser via ``xdg-open``. NEXUS
  does not drive the browser, cannot read what is on the page afterwards, and
  says so -- claiming otherwise would be the kind of invented capability §78
  forbids.
* ``browser.search`` runs a real web search and parses real results.
* ``net.fetch`` downloads a page and extracts readable text.

The security work here is in ``_assert_public``. ``policy.url_refusal`` blocks
private *hostnames*, but a public hostname can resolve to a private address, and
a public URL can redirect to one. Both are how a "fetch this page" tool becomes a
way to read a cloud metadata endpoint or a service bound to localhost. So the
host is resolved and every address checked with ``ipaddress``, and the check runs
again on every redirect hop rather than once at the start.
"""

from __future__ import annotations

import gzip
import ipaddress
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from typing import Any

from ..logging_setup import get_logger
from ..permissions import policy
from .registry import ToolResult, tool

log = get_logger("tools.browser")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 NEXUS/1.0"
)
MAX_DOWNLOAD_BYTES = 3_000_000
MAX_TEXT_CHARS = 60_000
FETCH_TIMEOUT_S = 20.0

# Content types worth extracting text from. Anything else is reported as a
# download we are not going to interpret, rather than decoded as mojibake.
TEXTUAL_TYPES = (
    "text/html", "text/plain", "application/json", "application/xml",
    "text/xml", "application/xhtml+xml", "text/markdown", "text/csv",
)


class BlockedAddress(ValueError):
    """The target resolved to an address NEXUS refuses to contact."""


def _assert_public(host: str) -> list[str]:
    """Resolve a hostname and refuse any private, loopback or link-local address.

    Returns the resolved addresses so they can be reported. Raises
    ``BlockedAddress`` when any resolved address is not a public one -- *any*,
    not *all*, because a host with one public and one private address is a
    classic DNS-rebinding shape.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedAddress(f"'{host}' could not be found: {exc.strerror or exc}") from None

    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        if raw in addresses:
            continue
        addresses.append(raw)
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise BlockedAddress(f"'{host}' resolved to something that is not an address: {raw}") from None
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise BlockedAddress(
                f"'{host}' resolves to {raw}, which is on a private or local network. "
                "NEXUS will not fetch it, because that would let a web request reach "
                "services running on this machine or your home network."
            )
    if not addresses:
        raise BlockedAddress(f"'{host}' has no usable address.")
    return addresses


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the policy and address checks on every redirect hop."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        refusal = policy.url_refusal(newurl)
        if refusal is not None:
            raise BlockedAddress(
                f"That page redirected to {newurl}, which NEXUS will not follow. {refusal.reason}"
            )
        host = urllib.parse.urlparse(newurl).hostname or ""
        _assert_public(host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_CheckedRedirectHandler())


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return body
    return body


def _download(url: str, *, timeout: float = FETCH_TIMEOUT_S) -> dict[str, Any]:
    """Fetch a URL with every guard applied. Raises BlockedAddress or URLError."""
    refusal = policy.url_refusal(url)
    if refusal is not None:
        raise BlockedAddress(f"{refusal.reason} {refusal.detail}".strip())

    parsed = urllib.parse.urlparse(url)
    addresses = _assert_public(parsed.hostname or "")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with _opener().open(request, timeout=timeout) as response:
        raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        truncated = len(raw) > MAX_DOWNLOAD_BYTES
        raw = raw[:MAX_DOWNLOAD_BYTES]
        body = _decompress(raw, response.headers.get("Content-Encoding", ""))
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        charset = "utf-8"
        if "charset=" in (response.headers.get("Content-Type") or ""):
            charset = (response.headers.get("Content-Type") or "").split("charset=")[-1].strip()
        return {
            "final_url": response.geturl(),
            "status": response.status,
            "content_type": content_type,
            "charset": charset,
            "body": body,
            "bytes": len(body),
            "truncated": truncated,
            "addresses": addresses,
        }


# ------------------------------------------------------------- text extraction

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "td", "th",
}


class _TextExtractor(HTMLParser):
    """Turn HTML into readable text, keeping paragraph structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return "\n".join(line.strip() for line in joined.splitlines()).strip()


def _extract(body: bytes, content_type: str, charset: str) -> tuple[str, str]:
    """Return (title, text) for a downloaded body."""
    try:
        decoded = body.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        decoded = body.decode("utf-8", errors="replace")

    if content_type in ("text/html", "application/xhtml+xml"):
        parser = _TextExtractor()
        try:
            parser.feed(decoded)
            parser.close()
        except Exception:  # noqa: BLE001 - malformed HTML must not fail the fetch
            return "", decoded[:MAX_TEXT_CHARS]
        return parser.title, parser.text()[:MAX_TEXT_CHARS]

    return "", decoded[:MAX_TEXT_CHARS]


# ---------------------------------------------------------------- browser.open


def _verify_open(params: dict[str, Any], result: ToolResult) -> tuple[bool, str]:
    """Confirm a browser process is actually running after the handoff.

    ``xdg-open`` exiting 0 only means a handler was found. It does not mean a
    browser started, and it can never mean the page rendered -- NEXUS has no view
    into the browser. So the check is narrow and the wording says what it covers.
    """
    if not result.data:
        return False, "Nothing was opened."
    try:
        proc = subprocess.run(
            ["pgrep", "-a", "-f", "firefox|chrome|chromium|brave|epiphany|vivaldi|opera"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return (
            True,
            "The desktop accepted the address. NEXUS cannot see the page that loads, "
            "so it cannot report what is on it.",
        )
    if proc.returncode == 0 and proc.stdout.strip():
        name = proc.stdout.strip().splitlines()[0].split()[1].rsplit("/", 1)[-1]
        return (
            True,
            f"Confirmed: {name} is running and was handed the address. NEXUS cannot see "
            "the page itself, so it cannot report what is on it.",
        )
    return (
        True,
        "The desktop accepted the address, but no browser process is visible yet. It may "
        "still be starting. NEXUS cannot see the page, so it cannot report what loaded.",
    )


@tool(
    "browser.open",
    title="Open a web page",
    description=(
        "Open a URL in the user's default browser. NEXUS cannot read the page "
        "afterwards or interact with it -- use fetch_page for that."
    ),
    capability="browser.open",
    idempotent=False,
    verifier=_verify_open,
    requires=("xdg-open",),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 4, "description": "Full https:// address."},
        },
        "required": ["url"],
    },
)
def browser_open(params: dict[str, Any]) -> ToolResult:
    url = str(params["url"]).strip()
    if "://" not in url:
        url = f"https://{url}"

    refusal = policy.url_refusal(url)
    if refusal is not None:
        return ToolResult(ok=False, error=f"{refusal.reason} {refusal.detail}".strip(),
                          summary=refusal.reason)

    try:
        proc = subprocess.run(["xdg-open", url], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(ok=False, error=f"Could not open the browser: {exc}")
    if proc.returncode != 0:
        return ToolResult(
            ok=False,
            error=(f"Could not open {url}: {proc.stderr.strip() or 'xdg-open failed'}. "
                   "This usually means no default browser is set."),
        )

    return ToolResult(
        ok=True,
        data={"url": url, "handler": "xdg-open"},
        summary=f"Opened {url} in your browser",
    )


# -------------------------------------------------------------- browser.search


class _DuckDuckGoParser(HTMLParser):
    """Parse the no-JavaScript DuckDuckGo results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._collect: str | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    @staticmethod
    def _href(attrs: list[tuple[str, str | None]]) -> str:
        for name, value in attrs:
            if name == "href" and value:
                return value
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": self._clean(self._href(attrs)), "snippet": ""}
            self._collect = "title"
        elif tag == "a" and "result__snippet" in classes:
            self._collect = "snippet"
        elif tag == "div" and "result__snippet" in classes:
            self._collect = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "div") and self._collect == "snippet" and self._current:
            self.results.append(self._current)
            self._current = None
            self._collect = None
        elif tag == "a" and self._collect == "title":
            self._collect = None

    def handle_data(self, data: str) -> None:
        if self._current is None or self._collect is None:
            return
        self._current[self._collect] += data

    @staticmethod
    def _clean(href: str) -> str:
        """DuckDuckGo wraps results in a redirect; recover the real address."""
        if href.startswith("//duckduckgo.com/l/") or "/l/?uddg=" in href:
            query = urllib.parse.urlparse(href).query
            target = urllib.parse.parse_qs(query).get("uddg")
            if target:
                return target[0]
        if href.startswith("//"):
            return f"https:{href}"
        return href

    def cleaned(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in self.results:
            url = item["url"].strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append(
                {
                    "title": re.sub(r"\s+", " ", item["title"]).strip(),
                    "url": url,
                    "snippet": re.sub(r"\s+", " ", item["snippet"]).strip(),
                }
            )
        return out


@tool(
    "browser.search",
    title="Search the web",
    description=(
        "Search the web and return the result titles, addresses and snippets. "
        "Use fetch_page afterwards to read one of the results in full."
    ),
    capability="browser.search",
    read_only=True,
    timeout_s=30.0,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["query"],
    },
)
def browser_search(params: dict[str, Any]) -> ToolResult:
    query = str(params["query"]).strip()
    limit = int(params.get("limit", 8))
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})

    try:
        response = _download(url, timeout=20.0)
    except BlockedAddress as exc:
        return ToolResult(ok=False, error=str(exc))
    except urllib.error.HTTPError as exc:
        return ToolResult(
            ok=False,
            error=f"The search provider returned {exc.code} ({exc.reason}), so there are no results.",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ToolResult(
            ok=False,
            error=f"Could not reach the search provider: {getattr(exc, 'reason', exc)}. "
                  "Check that this computer is online.",
        )

    html = response["body"].decode(response["charset"], errors="replace")
    parser = _DuckDuckGoParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    results = parser.cleaned()[:limit]

    if not results:
        # Zero parsed results and a 200 response means either a genuinely empty
        # search or a changed page layout. Those are different, and reporting
        # "nothing found" for the second would be a quiet lie (§79).
        blocked = "anomaly" in html.lower() or "unusual traffic" in html.lower()
        return ToolResult(
            ok=False,
            data={"query": query, "status": response["status"], "bytes": response["bytes"]},
            error=(
                "The search provider blocked this request as automated traffic."
                if blocked
                else f"No results could be read for '{query}'. The search page loaded "
                     "but nothing matched the expected layout, so NEXUS cannot tell "
                     "whether there are genuinely no results."
            ),
        )

    return ToolResult(
        ok=True,
        data={"query": query, "results": results, "count": len(results),
              "provider": "duckduckgo"},
        summary=f"Found {len(results)} result(s) for '{query}'",
        verified=True,
        verification=f"{len(results)} result(s) parsed from a live search response.",
    )


# ------------------------------------------------------------------- net.fetch


@tool(
    "net.fetch",
    title="Read a web page",
    description=(
        "Download a web page or API response and return its readable text. Use "
        "this to actually read a page's content, rather than just opening it."
    ),
    capability="net.fetch",
    read_only=True,
    timeout_s=30.0,
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 4},
            "max_characters": {
                "type": "integer", "minimum": 500, "maximum": MAX_TEXT_CHARS,
                "default": 20_000,
            },
        },
        "required": ["url"],
    },
)
def net_fetch(params: dict[str, Any]) -> ToolResult:
    url = str(params["url"]).strip()
    if "://" not in url:
        url = f"https://{url}"
    limit = int(params.get("max_characters", 20_000))

    try:
        response = _download(url)
    except BlockedAddress as exc:
        return ToolResult(ok=False, error=str(exc), summary="Address refused")
    except urllib.error.HTTPError as exc:
        return ToolResult(
            ok=False,
            data={"url": url, "status": exc.code},
            error=f"{url} returned {exc.code} ({exc.reason}).",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ToolResult(
            ok=False,
            error=f"Could not reach {url}: {getattr(exc, 'reason', exc)}",
        )

    content_type = response["content_type"]
    if content_type and content_type not in TEXTUAL_TYPES:
        return ToolResult(
            ok=False,
            data={"url": response["final_url"], "content_type": content_type,
                  "bytes": response["bytes"]},
            error=(
                f"{url} is {content_type}, not text NEXUS can read. It downloaded "
                f"{response['bytes']:,} bytes but there is no readable content to report."
            ),
        )

    title, text = _extract(response["body"], content_type, response["charset"])
    clipped = text[:limit]
    redirected = response["final_url"] != url

    return ToolResult(
        ok=True,
        data={
            "url": url,
            "final_url": response["final_url"],
            "redirected": redirected,
            "status": response["status"],
            "title": title,
            "text": clipped,
            "characters": len(clipped),
            "characters_available": len(text),
            "truncated": len(clipped) < len(text) or response["truncated"],
            "content_type": content_type,
        },
        summary=(
            f"Read {len(clipped):,} characters from {title or response['final_url']}"
            + (" (truncated)" if len(clipped) < len(text) else "")
        ),
        verified=True,
        verification=(
            f"HTTP {response['status']} from {response['final_url']}"
            + (" after a redirect" if redirected else "")
            + f", {response['bytes']:,} bytes received."
        ),
    )
