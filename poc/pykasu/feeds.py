"""RSS/Atom parsing and feed-entry normalization."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import urljoin

import feedparser

from .http_client import UnsafeUrlError, validate_source_url
from .models import FeedEntry
from .util import as_utc_text, fingerprint, html_to_text, limited_excerpt, normalize_text, normalize_url


class FeedParseError(RuntimeError):
    pass


def conditional_headers(source: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    if source.get("etag"):
        headers["If-None-Match"] = str(source["etag"])
    if source.get("last_modified"):
        headers["If-Modified-Since"] = str(source["last_modified"])
    return headers


def parse_feed_entries(source: dict[str, Any], body: bytes, limit: int) -> list[FeedEntry]:
    """Return at most the newest entries from one configured feed.

    Feedparser's ``bozo`` flag can be set for harmless malformed XML that still
    has usable entries, so it is only fatal when no entries were recovered.
    """

    parsed = feedparser.parse(body)
    raw_entries = list(parsed.entries or [])
    if not raw_entries and getattr(parsed, "bozo", False):
        raise FeedParseError("Feed could not be parsed")
    candidates: list[tuple[float, int, FeedEntry]] = []
    for position, entry in enumerate(raw_entries):
        candidate = _entry_from_parsed(source, entry)
        if candidate is None:
            continue
        timestamp = _entry_epoch(entry)
        # Earlier feed order breaks timestamp ties, which is a predictable
        # fallback for feeds with incomplete date metadata.
        candidates.append((timestamp if timestamp is not None else float("-inf"), -position, candidate))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen: list[FeedEntry] = []
    seen_urls: set[str] = set()
    for _, _, candidate in candidates:
        if candidate.normalized_url in seen_urls:
            continue
        seen_urls.add(candidate.normalized_url)
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def _entry_from_parsed(source: dict[str, Any], entry: Any) -> FeedEntry | None:
    raw_link = normalize_text(_value(entry, "link"))
    headline = html_to_text(_value(entry, "title"))
    if not raw_link or not headline:
        return None
    absolute_link = urljoin(str(source["feed_url"]), raw_link)
    try:
        # Performs cheap scheme/literal-private validation; DNS is checked
        # again by SafeHttpClient before any article retrieval.
        url = validate_source_url(absolute_link)
        normalized = normalize_url(url)
    except (ValueError, UnsafeUrlError):
        return None
    excerpt = _feed_excerpt(entry)
    published_at = _entry_datetime(entry)
    metadata = {
        "headline": headline,
        "published_at": published_at,
        "excerpt": excerpt,
        "url": normalized,
    }
    return FeedEntry(
        source_id=int(source["id"]),
        source_name=str(source["name"]),
        url=url,
        normalized_url=normalized,
        headline=headline,
        published_at=published_at,
        excerpt=excerpt,
        metadata_fingerprint=fingerprint(metadata),
    )


def _feed_excerpt(entry: Any) -> str:
    candidates: list[object] = [
        _value(entry, "summary"),
        _value(entry, "description"),
    ]
    content = _value(entry, "content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                candidates.append(part.get("value"))
            else:
                candidates.append(getattr(part, "value", None))
    for candidate in candidates:
        text = limited_excerpt(html_to_text(str(candidate or "")))
        if text:
            return text
    return ""


def _entry_datetime(entry: Any) -> str | None:
    epoch = _entry_epoch(entry)
    if epoch is None:
        return None
    return as_utc_text(datetime.fromtimestamp(epoch, UTC))


def _entry_epoch(entry: Any) -> float | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = _value(entry, field)
        if value:
            try:
                return float(calendar.timegm(value))
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _value(entry: Any, key: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(key)
    getter = getattr(entry, "get", None)
    if callable(getter):
        return getter(key)
    return getattr(entry, key, None)
