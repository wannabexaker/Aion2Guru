"""SSRF-safe HTTP fetching (DESIGN §16).

- http/https only, no credentials in URLs
- every resolved address must be globally routable (no private, loopback, link-local, CGNAT, metadata…)
- we connect to the *validated IP* (Host header + TLS SNI keep virtual hosting and cert checks working),
  so DNS rebinding between check and connect is impossible
- redirects are followed manually and re-validated hop by hop
- robots.txt respected, per-host politeness delay, size/type caps, conditional GET
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from guru.logging import get_logger
from guru.settings import WebSettings

log = get_logger(__name__)

ALLOWED_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
    "text/plain",
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchBlocked(Exception):
    """Refused by policy (SSRF, robots, scheme, type, size)."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str = ""
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def gone(self) -> bool:
        return self.status in (404, 410)

    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.content_type:
            charset = self.content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def validate_url(url: str) -> tuple[str, str, int]:
    """Returns (scheme, host, port) or raises FetchBlocked."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchBlocked(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise FetchBlocked("credentials in URL")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchBlocked("missing host")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise FetchBlocked(f"host not allowed: {host}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


class SafeFetcher:
    def __init__(
        self,
        cfg: WebSettings,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(verify=True)
        self.resolver = resolver or system_resolver
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def _resolve_public(self, host: str, port: int) -> str:
        try:
            ipaddress.ip_address(host)
            ips = [host]
        except ValueError:
            try:
                ips = await self.resolver(host, port)
            except OSError as exc:
                raise FetchBlocked(f"dns failure for {host}: {exc}") from exc
        if not ips:
            raise FetchBlocked(f"no address for {host}")
        bad = [ip for ip in ips if not is_public_ip(ip)]
        if bad:
            raise FetchBlocked(f"non-public address for {host}: {bad[0]}")
        return ips[0]

    async def _polite(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last_hit.get(host, 0.0) + self.cfg.min_host_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def allowed_by_robots(self, url: str) -> bool:
        scheme, host, port = validate_url(url)
        key = f"{scheme}://{host}:{port}"
        cached = self._robots.get(key)
        if cached is None or cached[0] < time.monotonic():
            parser: RobotFileParser | None
            ttl = 86400.0
            try:
                res = await self.fetch(f"{scheme}://{host}:{port}/robots.txt", check_robots=False)
                if res.status >= 500:
                    parser, ttl = None, 3600.0  # temporary failure → disallow for an hour
                else:
                    parser = RobotFileParser()
                    parser.parse(res.text().splitlines() if res.status == 200 else [])
            except (FetchBlocked, httpx.HTTPError):
                parser, ttl = None, 3600.0
            self._robots[key] = (time.monotonic() + ttl, parser)
            cached = self._robots[key]
        parser = cached[1]
        return parser is not None and parser.can_fetch(self.cfg.user_agent, url)

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        check_robots: bool = True,
    ) -> FetchResult:
        for _hop in range(self.cfg.max_redirects + 1):
            scheme, host, port = validate_url(url)
            ip = await self._resolve_public(host, port)
            if check_robots and not await self.allowed_by_robots(url):
                raise FetchBlocked(f"robots.txt disallows {url}")
            await self._polite(host)
            parts = urlsplit(url)
            netloc_ip = f"[{ip}]" if ":" in ip else ip
            default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
            target = urlunsplit(
                (scheme, netloc_ip if default_port else f"{netloc_ip}:{port}", parts.path or "/", parts.query, "")
            )
            headers = {
                "Host": host if default_port else f"{host}:{port}",
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.5",
            }
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            extensions = {"sni_hostname": host} if scheme == "https" else {}
            async with self.client.stream(
                "GET",
                target,
                headers=headers,
                extensions=extensions,
                follow_redirects=False,
                timeout=self.cfg.timeout_s,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    continue
                result_headers = {k.lower(): v for k, v in resp.headers.items()}
                if resp.status_code == 304 or resp.status_code >= 400:
                    return FetchResult(url, resp.status_code, headers=result_headers)
                ctype = resp.headers.get("content-type", "").lower()
                if not any(ctype.startswith(t) for t in ALLOWED_TYPES):
                    raise FetchBlocked(f"content type not allowed: {ctype or 'unknown'}")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > self.cfg.max_bytes:
                    raise FetchBlocked(f"too large: {declared} bytes")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.cfg.max_bytes:
                        raise FetchBlocked(f"too large: >{self.cfg.max_bytes} bytes")
                    chunks.append(chunk)
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    content_type=ctype,
                    body=b"".join(chunks),
                    etag=resp.headers.get("etag"),
                    last_modified=resp.headers.get("last-modified"),
                    headers=result_headers,
                )
        raise FetchBlocked("too many redirects")
