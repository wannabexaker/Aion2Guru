"""Main-text extraction, dating, language, near-duplicate fingerprints, chunking, feeds & sitemaps."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import feedparser
import trafilatura
from defusedxml import ElementTree
from htmldate import find_date

from guru.core.text import ENGLISH_STOPWORDS, detect_script, tokens


@dataclass(frozen=True)
class ExtractedDoc:
    title: str | None
    text: str
    site_name: str | None
    published_at: datetime | None
    updated_at: datetime | None
    date_confidence: str  # metadata | heuristic | unknown
    lang: str | None  # 'en' | 'el' | None (unsupported → skipped, D-03)


def _to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def supported_language(text: str) -> str | None:
    """Only English and Greek sources are used (D-03)."""
    script = detect_script(text)
    if script == "greek":
        return "el"
    if script != "latin":
        return None
    toks = tokens(text[:5000])
    if not toks:
        return None
    ratio = sum(1 for t in toks if t in ENGLISH_STOPWORDS) / len(toks)
    return "en" if ratio >= 0.12 else None


def extract_document(html: str, url: str) -> ExtractedDoc | None:
    doc = trafilatura.bare_extraction(
        html, url=url, with_metadata=True, include_comments=False, include_tables=True, favor_precision=True
    )
    text = (getattr(doc, "text", None) or "").strip() if doc is not None else ""
    if len(text) < 80:
        return None
    published = _to_dt(find_date(html, original_date=True, extensive_search=False, outputformat="%Y-%m-%d"))
    updated = _to_dt(find_date(html, original_date=False, extensive_search=False, outputformat="%Y-%m-%d"))
    confidence = "metadata" if (published or updated) else "unknown"
    if not (published or updated):
        guessed = _to_dt(find_date(html, extensive_search=True, outputformat="%Y-%m-%d"))
        if guessed:
            published, confidence = guessed, "heuristic"
    return ExtractedDoc(
        title=getattr(doc, "title", None),
        text=text,
        site_name=getattr(doc, "sitename", None),
        published_at=published,
        updated_at=updated if updated and updated != published else None,
        date_confidence=confidence,
        lang=supported_language(text),
    )


# ---------------------------------------------------------------- near-duplicates

# Max differing bits for "same document" (syndicated copy, minor edits). Calibrated on multi-paragraph pages.
NEAR_DUP_BITS = 5


def simhash(text: str, shingle: int = 3) -> int:
    toks = tokens(text)
    if len(toks) < shingle:
        toks = toks + [""] * (shingle - len(toks))
    weights = [0] * 64
    for i in range(len(toks) - shingle + 1):
        h = int.from_bytes(hashlib.blake2b(" ".join(toks[i : i + shingle]).encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if weights[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


# ---------------------------------------------------------------- chunking

_HEADING = re.compile(r"^(#{1,6}\s+.+|[A-Z0-9][^\n]{0,80})$")


def chunk_text(text: str, max_chars: int = 1600) -> list[str]:
    """Paragraph-aware chunks ≤ max_chars; never splits inside a paragraph unless it is too long."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for p in paragraphs:
        while len(p) > max_chars:
            cut = p.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if size + len(p) > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(p)
        size += len(p) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# ---------------------------------------------------------------- feeds & sitemaps


def feed_entries(xml: bytes, limit: int = 20) -> list[tuple[str, datetime | None]]:
    parsed = feedparser.parse(xml)
    out: list[tuple[str, datetime | None]] = []
    for e in parsed.entries[:limit]:
        link = e.get("link")
        if not link:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        y, mo, d, h, mi, s = stamp[:6] if stamp else (0, 0, 0, 0, 0, 0)
        out.append((link, datetime(y, mo, d, h, mi, s, tzinfo=UTC) if stamp else None))
    return out


def sitemap_urls(xml: bytes, limit: int = 200) -> list[tuple[str, datetime | None]]:
    root = ElementTree.fromstring(xml)
    out: list[tuple[str, datetime | None]] = []
    for el in root.iter():
        if not el.tag.endswith("url") and not el.tag.endswith("sitemap"):
            continue
        loc = next((c.text for c in el if c.tag.endswith("loc")), None)
        lastmod = next((c.text for c in el if c.tag.endswith("lastmod")), None)
        if loc:
            out.append((loc.strip(), _to_dt(lastmod.strip()) if lastmod else None))
        if len(out) >= limit:
            break
    return out
