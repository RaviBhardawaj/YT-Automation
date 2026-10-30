"""Outbound HTTP with guard rails.

* Only public http(s) hosts (no localhost / LAN / metadata IPs) for anything whose URL
  came from a search result or a model — re-checked on every redirect hop.
* robots.txt is honoured for ordinary web pages (APIs and RSS feeds are published for
  programmatic use and are fetched directly).
* Response sizes are capped; downloads stream to disk.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from . import config

log = logging.getLogger(__name__)
ROBOTS_TOKEN = "YTAutomationStudio"


class FetchError(Exception):
    pass


# ------------------------------------------------------------------ IPv6 fallback

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - mirrors socket API
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


def configure_ip_preference() -> bool:
    """Python's HTTP stack tries IPv6 first and waits a full connect timeout when the
    network advertises IPv6 but cannot route it (curl/browsers race both instead).
    IPv4 is always preferred process-wide (IPv6 stays the fallback): on this phone-hotspot network IPv6 sometimes
    connects and then stalls mid-transfer, so a passing quick test proves nothing. Returns True if IPv6 connects."""
    socket.getaddrinfo = _ipv4_first
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(("2001:4860:4860::8888", 443))
        return True
    except OSError:
        log.info("IPv6 unreachable — preferring IPv4 for outbound connections")
        return False
    finally:
        s.close()


class BlockedURL(FetchError):
    pass


def user_agent() -> str:
    """Wikimedia and other public APIs ask clients to include contact details in the
    User-Agent. Optional: Settings › Research › contact."""
    from . import settings
    contact = (settings.get("research.contact") or "").strip()
    return config.USER_AGENT[:-1] + f"; contact: {contact})" if contact else config.USER_AGENT


def _client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent(), "Accept-Language": "en,hi;q=0.8"},
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=False,
    )


def check_public(url: str) -> None:
    """Raise BlockedURL for non-public targets, FetchError when DNS itself fails
    (flaky connections, e.g. a phone hotspot, should not look like a security block)."""
    try:
        p = urlparse(url)
    except ValueError as e:
        raise BlockedURL(f"invalid URL: {url}") from e
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedURL(f"refusing non-http(s) URL: {url}")
    host = p.hostname
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")):
        raise BlockedURL(f"refusing local host: {host}")
    infos = None
    for attempt in range(3):
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
            break
        except socket.gaierror as e:
            if attempt == 2:
                raise FetchError(f"DNS lookup failed for {host} — check the internet connection") from e
            time.sleep(1.0 + attempt)
    for info in infos or []:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise BlockedURL(f"refusing non-public address for {host}")


check_public_url = check_public


def is_public_url(url: str) -> bool:
    try:
        check_public(url)
        return True
    except FetchError:
        return False


# ------------------------------------------------------------------ robots.txt

_robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, bool]] = {}
_robots_lock = threading.Lock()


def robots_allowed(url: str) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    now = time.time()
    with _robots_lock:
        cached = _robots.get(origin)
    if cached and now - cached[0] < 6 * 3600:
        _, parser, allow_all = cached
    else:
        parser, allow_all = None, False
        try:
            with _client(10) as c:
                r = c.get(origin + "/robots.txt")
                hops = 0
                while r.is_redirect and hops < 3 and is_public_url(str(r.next_request.url)):
                    r = c.get(r.next_request.url)
                    hops += 1
            if r.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(r.text.splitlines())
            elif 400 <= r.status_code < 500:
                allow_all = True          # RFC 9309: robots.txt unavailable -> allowed
            else:
                allow_all = False         # server error -> assume disallowed
        except httpx.HTTPError:
            allow_all = False
        with _robots_lock:
            _robots[origin] = (now, parser, allow_all)
    if parser is not None:
        return parser.can_fetch(ROBOTS_TOKEN, url)
    return allow_all


# ------------------------------------------------------------------ politeness

# Minimum spacing between requests to the same host (seconds). Wikimedia asks API
# clients to keep request rates low; everything else gets a light default.
_HOST_INTERVAL = {"wikipedia.org": 1.0, "wikimedia.org": 1.0, "news.google.com": 0.5}
_host_next: dict[str, float] = {}
_host_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = urlparse(url).hostname or ""
    gap = next((v for k, v in _HOST_INTERVAL.items() if host == k or host.endswith("." + k)), 0.2)
    key = next((k for k in _HOST_INTERVAL if host == k or host.endswith("." + k)), host)
    with _host_lock:
        now = time.monotonic()
        start = max(now, _host_next.get(key, 0.0))
        _host_next[key] = start + gap
    if start > now:
        time.sleep(start - now)


# ------------------------------------------------------------------ requests

def _request(method: str, url: str, *, check_public: bool, max_bytes: int, timeout: float,
             **kwargs: Any) -> httpx.Response:
    hops = 0
    retried_429 = False
    with _client(timeout) as c:
        while True:
            if check_public:
                check_public_url(url)
            _throttle(url)
            req = c.build_request(method, url, **kwargs)
            r = c.send(req, stream=True)
            if r.status_code == 429 and not retried_429:
                wait = r.headers.get("retry-after", "")
                r.close()
                retried_429 = True
                time.sleep(min(float(wait) if wait.replace(".", "", 1).isdigit() else 5.0, 20.0))
                continue
            if r.is_redirect and hops < 5:
                nxt = urljoin(url, r.headers.get("location", ""))
                r.close()
                url, hops = nxt, hops + 1
                method = "GET" if method == "POST" and r.status_code in (301, 302, 303) else method
                continue
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    r.close()
                    raise FetchError(f"response from {urlparse(url).netloc} exceeded {max_bytes} bytes")
            r.close()
            r._content = bytes(body)  # make .text/.json() work after streaming
            return r


def get_json(url: str, params: dict | None = None, headers: dict | None = None, *,
             timeout: float = 25.0, max_bytes: int = 8_000_000, check_public: bool = True) -> Any:
    r = _request("GET", url, params=params, headers=headers or {}, check_public=check_public,
                 max_bytes=max_bytes, timeout=timeout)
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} from {urlparse(url).netloc}: {r.text[:300]}")
    return r.json()


def post_json(url: str, payload: Any, headers: dict | None = None, *, timeout: float = 60.0,
              max_bytes: int = 8_000_000, check_public: bool = True) -> Any:
    r = _request("POST", url, json=payload, headers=headers or {}, check_public=check_public,
                 max_bytes=max_bytes, timeout=timeout)
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} from {urlparse(url).netloc}: {r.text[:300]}")
    return r.json()


def get_text(url: str, *, respect_robots: bool = True, timeout: float = 20.0,
             max_bytes: int = 3_000_000) -> tuple[str, str]:
    """Fetch a web page or feed. Returns (content_type, text)."""
    if respect_robots and not robots_allowed(url):
        raise BlockedURL(f"robots.txt disallows {url}")
    r = _request("GET", url, check_public=True, max_bytes=max_bytes, timeout=timeout)
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    return r.headers.get("content-type", ""), r.text


def download(url: str, dest: Path, *, max_bytes: int, allowed_types: tuple[str, ...],
             headers: dict | None = None, timeout: float = 120.0) -> Path:
    """Stream a file to disk, enforcing size and content-type."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    hops = 0
    retried_429 = False
    with _client(timeout) as c:
        while True:
            check_public_url(url)
            _throttle(url)
            with c.stream("GET", url, headers=headers or {}) as r:
                if r.is_redirect and hops < 5:
                    url, hops = urljoin(url, r.headers.get("location", "")), hops + 1
                    continue
                if r.status_code == 429 and not retried_429:
                    retried_429 = True
                    wait = r.headers.get("retry-after", "")
                    time.sleep(min(float(wait) if wait.replace(".", "", 1).isdigit() else 5.0, 20.0))
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} downloading {urlparse(url).netloc}")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if allowed_types and not any(ctype.startswith(t) for t in allowed_types):
                    raise FetchError(f"unexpected content-type {ctype!r}")
                size = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 16):
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError(f"download exceeded {max_bytes} bytes")
                        fh.write(chunk)
            break
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------ HTML -> text

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "br", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = 12000) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # malformed HTML: keep what we have
        pass
    text = "".join(p.parts)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) > 40]          # drop menus / boilerplate fragments
    return p.title.strip()[:300], "\n".join(lines)[:limit]
