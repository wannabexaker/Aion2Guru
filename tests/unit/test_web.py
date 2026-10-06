from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from guru.settings import WebSettings
from guru.web.extract import (
    NEAR_DUP_BITS,
    chunk_text,
    extract_document,
    feed_entries,
    hamming,
    simhash,
    sitemap_urls,
    supported_language,
)
from guru.web.fetcher import FetchBlocked, SafeFetcher, is_public_ip, validate_url

PUBLIC = "93.184.216.34"
ARTICLE = """<html><head><title>Patch 1.2 notes</title>
<meta property="article:published_time" content="2026-09-01T10:00:00Z"></head>
<body><nav>menu</nav><article><h1>Patch 1.2</h1>
<p>The Fire Temple boss now respawns every 4 hours instead of 6. The entry level is now 45 for all players.</p>
<p>Enchanting above +10 can fail and the item may lose one level. This is how the system works now.</p>
</article><footer>copyright</footer></body></html>"""


def fetcher(
    handler: Callable[[httpx.Request], httpx.Response], resolve: dict[str, list[str]] | None = None, **cfg: Any
) -> SafeFetcher:
    table = resolve or {}

    async def resolver(host: str, port: int) -> list[str]:
        return table.get(host, [PUBLIC])

    settings = WebSettings(min_host_interval_s=0, **cfg)
    return SafeFetcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver)


def site(pages: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        key = f"{req.headers['host']}{req.url.path}"
        return pages.get(key, httpx.Response(404))

    return handler


def html(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


def test_ip_and_url_validation() -> None:
    for ip in (
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
    ):
        assert not is_public_ip(ip), ip
    assert is_public_ip(PUBLIC)
    for bad in ("file:///etc/passwd", "ftp://x.y", "http://user:pw@x.y/", "http://localhost/", "http://svc.internal/"):
        with pytest.raises(FetchBlocked):
            validate_url(bad)


async def test_fetch_connects_to_validated_ip_with_host_header() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        seen["url"] = str(req.url)
        seen["host"] = req.headers["host"]
        return html(ARTICLE, etag='"v1"')

    res = await fetcher(handler).fetch("http://wiki.example/page")
    assert seen == {"url": f"http://{PUBLIC}/page", "host": "wiki.example"}
    assert res.status == 200 and res.etag == '"v1"' and b"Fire Temple" in res.body


async def test_private_or_mixed_dns_is_blocked() -> None:
    f = fetcher(site({}), resolve={"evil.example": ["10.0.0.1"], "rebind.example": [PUBLIC, "127.0.0.1"]})
    for host in ("evil.example", "rebind.example"):
        with pytest.raises(FetchBlocked, match="non-public"):
            await f.fetch(f"http://{host}/", check_robots=False)
    with pytest.raises(FetchBlocked):
        await f.fetch("http://169.254.169.254/latest/meta-data", check_robots=False)


async def test_redirect_to_private_is_blocked() -> None:
    pages = {"good.example/go": httpx.Response(302, headers={"location": "http://internal.example/admin"})}
    f = fetcher(site(pages), resolve={"internal.example": ["192.168.0.10"]})
    with pytest.raises(FetchBlocked, match="non-public"):
        await f.fetch("http://good.example/go", check_robots=False)


async def test_robots_type_size_and_conditional_get() -> None:
    pages = {
        "site.example/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
        "site.example/ok": html(ARTICLE),
        "site.example/private/x": html(ARTICLE),
        "site.example/bin": httpx.Response(200, content=b"\x00" * 10, headers={"content-type": "application/zip"}),
        "site.example/big": html("x" * 5000),
    }
    f = fetcher(site(pages), max_bytes=4000)
    assert (await f.fetch("http://site.example/ok")).status == 200
    with pytest.raises(FetchBlocked, match="robots"):
        await f.fetch("http://site.example/private/x")
    with pytest.raises(FetchBlocked, match="content type"):
        await f.fetch("http://site.example/bin")
    with pytest.raises(FetchBlocked, match="too large"):
        await f.fetch("http://site.example/big")

    def conditional(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        assert req.headers.get("if-none-match") == '"v1"'
        return httpx.Response(304)

    assert (await fetcher(conditional).fetch("http://site.example/ok", etag='"v1"')).not_modified


def test_extract_document_dates_language() -> None:
    doc = extract_document(ARTICLE, "https://news.example/patch-1-2")
    assert doc is not None and doc.lang == "en"
    assert "respawns every 4 hours" in doc.text and "menu" not in doc.text
    assert doc.published_at is not None and doc.published_at.date().isoformat() == "2026-09-01"
    assert doc.date_confidence == "metadata"


def test_supported_languages_only_en_el() -> None:
    assert supported_language("The boss is in the temple and it drops a sword for the party.") == "en"
    assert supported_language("Ο boss βρίσκεται στο ναό και ρίχνει ένα σπαθί.") == "el"
    assert supported_language("보스는 사원에 있습니다") is None
    assert supported_language("Der Boss ist im Tempel und lässt ein Schwert fallen.") is None


LONG = " ".join(
    f"Section {i}: the {w} dungeon rewards players who clear it with {i + 2} tokens, and the boss guarding the "
    f"inner sanctum uses a {w} shield that must be broken before damage applies."
    for i, w in enumerate(["fire", "ice", "storm", "shadow", "light", "earth", "void", "iron", "jade", "ember"])
)


def test_simhash_detects_near_duplicates() -> None:
    syndicated = LONG.replace("tokens", "coins", 1) + " Originally published on the official site."
    unrelated = " ".join(
        f"Market note {i}: crafting {w} armor costs kinah and takes time to gather materials."
        for i, w in enumerate(["silk", "hide", "ore", "wood", "gem", "bone", "salt", "sand"])
    )
    assert hamming(simhash(LONG), simhash(syndicated)) <= NEAR_DUP_BITS
    assert hamming(simhash(LONG), simhash(unrelated)) > 10


def test_chunking_respects_limits() -> None:
    text = "\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(20))
    chunks = chunk_text(text, max_chars=800)
    assert all(len(c) <= 800 for c in chunks) and len(chunks) > 5
    assert "Paragraph 0." in chunks[0]


def test_feed_and_sitemap_parsing_and_xml_bomb() -> None:
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>a</title><link>https://n.example/a</link><pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate></item>
      <item><title>b</title><link>https://n.example/b</link></item></channel></rss>"""
    entries = feed_entries(rss)
    assert [u for u, _ in entries] == ["https://n.example/a", "https://n.example/b"]
    assert entries[0][1] is not None and entries[0][1].year == 2026
    sm = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://w.example/x</loc><lastmod>2026-08-01</lastmod></url></urlset>"""
    assert sitemap_urls(sm)[0][0] == "https://w.example/x"
    bomb = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>
      <urlset><url><loc>&lol2;</loc></url></urlset>"""
    with pytest.raises(Exception, match=r"(?i)entit"):
        sitemap_urls(bomb)
