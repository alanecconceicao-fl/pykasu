"""Deterministic collaborators and fixtures used by the offline test suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html import escape
from threading import Event, Lock
from typing import Any, Mapping

from pykasu.http_client import FetchError
from pykasu.models import FetchResponse, FeedEntry, NumberedParagraph
from pykasu.summaries import EvidenceReference, ItemSummary, validate_summary
from pykasu.util import as_utc_text, fingerprint, normalize_text, normalize_url, utc_now


def recent_rfc2822(*, hours_ago: int = 0) -> str:
    """Return a feed-compatible UTC date inside the normal acceptance window."""

    return format_datetime(utc_now() - timedelta(hours=hours_ago), usegmt=True)


def rss_feed(items: list[Mapping[str, str | None]]) -> bytes:
    """Make a small RSS document without consulting a network service."""

    rendered: list[str] = []
    for item in items:
        published = item.get("published")
        published_xml = f"<pubDate>{escape(published or '')}</pubDate>" if published else ""
        rendered.append(
            "<item>"
            f"<title>{escape(item['title'] or '')}</title>"
            f"<link>{escape(item['url'] or '')}</link>"
            f"<description>{escape(item.get('excerpt') or '')}</description>"
            f"{published_xml}"
            "</item>"
        )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<rss version=\"2.0\"><channel><title>Fixture feed</title>"
        + "".join(rendered)
        + "</channel></rss>"
    ).encode("utf-8")


def response(url: str, body: bytes, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> FetchResponse:
    return FetchResponse(url=url, status_code=status_code, headers=dict(headers or {}), body=body)


class FakeFeedClient:
    """A thread-safe fake feed client with optional response sequences."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._positions: dict[str, int] = {}
        self._lock = Lock()

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        with self._lock:
            self.calls.append((url, dict(headers or {})))
            if url not in self.responses:
                raise AssertionError(f"Unexpected fake feed URL: {url}")
            configured = self.responses[url]
            if isinstance(configured, list):
                position = self._positions.get(url, 0)
                configured = configured[min(position, len(configured) - 1)]
                self._positions[url] = position + 1
        if callable(configured):
            configured = configured(url, headers or {})
        if isinstance(configured, BaseException):
            raise configured
        if not isinstance(configured, FetchResponse):
            raise AssertionError(f"Fake response for {url} was not a FetchResponse")
        return configured


class FakeArticleExtractor:
    """In-memory article texts or deliberate extraction failures."""

    def __init__(self, outcomes: Mapping[str, str | BaseException] | None = None) -> None:
        self.outcomes = dict(outcomes or {})
        self.calls: list[str] = []

    def extract(self, url: str) -> str:
        self.calls.append(url)
        outcome = self.outcomes.get(
            url,
            "The fixture article provides enough source detail for a short, traceable summary.",
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingArticleExtractor(FakeArticleExtractor):
    """Lets a test observe the visible pending state during enrichment."""

    def __init__(self, article_text: str) -> None:
        super().__init__()
        self.article_text = article_text
        self.started = Event()
        self.release = Event()

    def extract(self, url: str) -> str:
        self.calls.append(url)
        self.started.set()
        if not self.release.wait(timeout=4):
            raise FetchError("Test extraction was not released")
        return self.article_text


class BlockingFeedClient(FakeFeedClient):
    """Keeps the refresh worker active while requests join its single flight."""

    def __init__(self, feed_url: str, feed_response: FetchResponse) -> None:
        super().__init__({feed_url: feed_response})
        self.started = Event()
        self.release = Event()

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        self.started.set()
        if not self.release.wait(timeout=4):
            raise FetchError("Test feed fetch was not released")
        return super().fetch(url, headers)


class ValidSummaryProvider:
    """A validated-shape model adapter that never reaches a real provider."""

    def __init__(self) -> None:
        self.calls = 0

    def summary_for_item(
        self, metadata: Mapping[str, object], numbered_paragraphs: list[NumberedParagraph]
    ) -> ItemSummary:
        self.calls += 1
        paragraph = numbered_paragraphs[0]
        quote = normalize_text(paragraph.text)[:120].rstrip()
        return ItemSummary(
            summary="The source reports a development described in the supplied material.",
            insufficient_information=False,
            evidence=[EvidenceReference(paragraph_id=paragraph.paragraph_id, quote=quote)],
        )


class InvalidEvidenceProvider:
    """Models a provider adapter rejecting a fabricated evidence quote."""

    def __init__(self) -> None:
        self.calls = 0

    def summary_for_item(
        self, metadata: Mapping[str, object], numbered_paragraphs: list[NumberedParagraph]
    ) -> ItemSummary:
        self.calls += 1
        response = ItemSummary(
            summary="The source reports a development.",
            insufficient_information=False,
            evidence=[EvidenceReference(paragraph_id=1, quote="fabricated evidence")],
        )
        # Production's Groq adapter validates the raw response before it is
        # handed to RefreshManager. This fake follows that same boundary.
        return validate_summary(response, numbered_paragraphs)


def database_entry(
    *,
    source_id: int,
    source_name: str,
    url: str,
    headline: str,
    published_at: str | None = None,
    excerpt: str = "A sufficiently detailed feed excerpt supports offline test coverage.",
) -> FeedEntry:
    """Construct a deterministic entry for tests exercising persistence directly."""

    normalized_url = normalize_url(url)
    metadata = {
        "headline": headline,
        "published_at": published_at,
        "excerpt": excerpt,
        "url": normalized_url,
    }
    return FeedEntry(
        source_id=source_id,
        source_name=source_name,
        url=url,
        normalized_url=normalized_url,
        headline=headline,
        published_at=published_at,
        excerpt=excerpt,
        metadata_fingerprint=fingerprint(metadata),
    )


def utc_hours_ago(hours: int) -> str:
    return as_utc_text(datetime.now(UTC) - timedelta(hours=hours))
