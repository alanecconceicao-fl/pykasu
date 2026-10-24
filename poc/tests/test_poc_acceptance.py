"""Offline coverage for the acceptance gates in ``POC-PLAN.md``.

These tests deliberately use feed, article, and model fakes injected through
``create_app``.  They make no DNS, HTTP, browser, or API-provider requests.
Run them from ``poc/`` with ``python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

from pykasu.app import create_app
from pykasu.backup import restore_backup, sqlite_backup
from pykasu.config import Settings
from pykasu.db import Database
from pykasu.http_client import (
    AccessRestrictedError,
    ArticleExtractor,
    FetchError,
    ResponseTooLargeError,
    UnsafeUrlError,
    _read_bounded_body,
    validate_source_url,
)
from pykasu.models import FetchResponse, NumberedParagraph
from pykasu.summaries import EvidenceReference, InvalidSummaryError, ItemSummary, validate_summary
from pykasu.util import as_utc_text, utc_now

from tests.helpers import (
    BlockingArticleExtractor,
    BlockingFeedClient,
    FakeArticleExtractor,
    FakeFeedClient,
    InvalidEvidenceProvider,
    ValidSummaryProvider,
    database_entry,
    recent_rfc2822,
    response,
    rss_feed,
    utc_hours_ago,
)


class AppHarness:
    """Owns an application and gives its background worker time to finish."""

    def __init__(self, app: Any, temporary_directory: tempfile.TemporaryDirectory[str]) -> None:
        self.app = app
        self.temporary_directory = temporary_directory
        self.database = app.extensions["pykasu_database"]
        self.manager = app.extensions["pykasu_refresh_manager"]
        self.settings = app.extensions["pykasu_settings"]

    def close(self) -> None:
        thread = self.manager._active_thread  # Test-only cleanup of a daemon worker.
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self.temporary_directory.cleanup()


class PocAcceptanceTests(unittest.TestCase):
    """Tests numbered to track the 13 plan acceptance requirements."""

    def setUp(self) -> None:
        self._harnesses: list[AppHarness] = []

    def tearDown(self) -> None:
        for harness in reversed(self._harnesses):
            harness.close()

    def make_app(
        self,
        *,
        feed_client: Any,
        article_extractor: Any | None = None,
        summary_provider: Any | None = None,
        groq_api_key: str | None = None,
        **setting_overrides: Any,
    ) -> AppHarness:
        temporary_directory = tempfile.TemporaryDirectory()
        values: dict[str, Any] = {
            "data_dir": Path(temporary_directory.name),
            "groq_api_key": groq_api_key,
        }
        values.update(setting_overrides)
        settings = Settings(**values)
        app = create_app(
            settings,
            feed_client=feed_client,
            article_extractor=article_extractor or FakeArticleExtractor(),
            summary_provider=summary_provider,
        )
        app.config.update(TESTING=True)
        harness = AppHarness(app, temporary_directory)
        self._harnesses.append(harness)
        return harness

    def add_source(
        self,
        harness: AppHarness,
        name: str,
        feed_url: str,
        *,
        enabled: bool = True,
        extract_articles: bool = False,
    ) -> int:
        return harness.database.create_source(name, feed_url, enabled, extract_articles)

    def wait_for_run(
        self, harness: AppHarness, run_id: int, *, timeout: float = 5
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = harness.manager.status()
            run = status["run"]
            if run and int(run["id"]) == run_id and run["status"] != "running" and not status["active"]:
                return run
            time.sleep(0.01)
        self.fail(f"Refresh run {run_id} did not complete")

    def refresh(self, harness: AppHarness, *, timeout: float = 5) -> dict[str, Any]:
        return self.wait_for_run(harness, harness.manager.start_or_join(), timeout=timeout)

    def query_rows(self, harness: AppHarness, *, source_id: int | None = None, search: str | None = None) -> list[dict[str, Any]]:
        return harness.database.query_updates(
            cutoff=utc_hours_ago(24 * 8),
            source_id=source_id,
            search=search,
            acknowledged_sequence=0,
        )

    def test_01_configured_feeds_render_one_chronological_view_with_links_and_timestamps(self) -> None:
        alpha_url = "https://alpha.example.test/feed.xml"
        beta_url = "https://beta.example.test/feed.xml"
        alpha_body = rss_feed(
            [
                {
                    "title": "Alpha older update",
                    "url": "https://alpha.example.test/articles/older",
                    "published": recent_rfc2822(hours_ago=5),
                    "excerpt": "Alpha published enough detail to be shown in the chronological view.",
                },
                {
                    "title": "Alpha newest update",
                    "url": "https://alpha.example.test/articles/newest",
                    "published": recent_rfc2822(hours_ago=1),
                    "excerpt": "Alpha published a newer item with meaningful fixture detail for testing.",
                },
            ]
        )
        beta_body = rss_feed(
            [
                {
                    "title": "Beta middle update",
                    "url": "https://beta.example.test/articles/middle",
                    "published": recent_rfc2822(hours_ago=3),
                    "excerpt": "Beta contributed a separate item to the combined chronological feed.",
                }
            ]
        )
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {alpha_url: response(alpha_url, alpha_body), beta_url: response(beta_url, beta_body)}
            )
        )
        self.add_source(harness, "Alpha", alpha_url)
        self.add_source(harness, "Beta", beta_url)

        self.refresh(harness)

        rows = self.query_rows(harness)
        self.assertEqual([row["headline"] for row in rows], ["Alpha newest update", "Beta middle update", "Alpha older update"])
        self.assertEqual([row["source_names"] for row in rows], ["Alpha", "Beta", "Alpha"])
        self.assertTrue(all(row["published_at"] for row in rows))

        page = harness.app.test_client().get("/updates?window=7d")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="https://alpha.example.test/articles/newest"', html)
        self.assertIn('href="https://beta.example.test/articles/middle"', html)
        self.assertIn(rows[0]["published_at"], html)

    def test_02_identical_urls_deduplicate_but_separate_event_urls_remain_distinct(self) -> None:
        alpha_url = "https://alpha.example.test/feed.xml"
        beta_url = "https://beta.example.test/feed.xml"
        common_url = "https://publisher.example.test/articles/common-event"
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    alpha_url: response(
                        alpha_url,
                        rss_feed(
                            [
                                {
                                    "title": "Shared publisher item",
                                    "url": common_url,
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "A shared feed item contains enough text to show as an update.",
                                },
                                {
                                    "title": "First report of an event",
                                    "url": "https://alpha.example.test/articles/event",
                                    "published": recent_rfc2822(hours_ago=2),
                                    "excerpt": "This is a distinct URL even though it covers the same event.",
                                },
                            ]
                        ),
                    ),
                    beta_url: response(
                        beta_url,
                        rss_feed(
                            [
                                {
                                    "title": "Shared publisher item",
                                    "url": common_url + "#feed-copy",
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "A duplicate URL must only create one persistent source item.",
                                },
                                {
                                    "title": "Second report of an event",
                                    "url": "https://beta.example.test/articles/event",
                                    "published": recent_rfc2822(hours_ago=2),
                                    "excerpt": "A different publisher URL remains a distinct source item.",
                                },
                            ]
                        ),
                    ),
                }
            )
        )
        self.add_source(harness, "Alpha", alpha_url)
        self.add_source(harness, "Beta", beta_url)

        self.refresh(harness)

        rows = self.query_rows(harness)
        self.assertEqual(len(rows), 3)
        shared = next(row for row in rows if row["url"] == common_url)
        self.assertIn("Alpha", shared["source_names"])
        self.assertIn("Beta", shared["source_names"])
        self.assertEqual(
            {row["url"] for row in rows if "report of an event" in row["headline"]},
            {"https://alpha.example.test/articles/event", "https://beta.example.test/articles/event"},
        )
        with harness.database.connection() as conn:
            associations = conn.execute(
                "SELECT count(*) FROM item_sources WHERE item_id = ?", (shared["id"],)
            ).fetchone()[0]
        self.assertEqual(associations, 2)

    def test_03_item_is_readable_while_its_summary_is_pending(self) -> None:
        feed_url = "https://pending.example.test/feed.xml"
        article_url = "https://pending.example.test/articles/one"
        extractor = BlockingArticleExtractor(
            "The article provides a detailed source account that remains available only in memory during processing."
        )
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    feed_url: response(
                        feed_url,
                        rss_feed(
                            [
                                {
                                    "title": "Visible before enrichment",
                                    "url": article_url,
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "The feed itself also has adequate context for this pending-item test.",
                                }
                            ]
                        ),
                    )
                }
            ),
            article_extractor=extractor,
            summary_provider=ValidSummaryProvider(),
            groq_api_key="test-key-not-a-real-credential",
        )
        self.add_source(harness, "Pending source", feed_url, extract_articles=True)

        run_id = harness.manager.start_or_join()
        self.assertTrue(extractor.started.wait(timeout=3), "refresh never reached article extraction")
        rows = self.query_rows(harness)
        self.assertEqual(rows[0]["summary_status"], "processing")
        page = harness.app.test_client().get("/updates?window=7d")
        self.assertIn("Visible before enrichment", page.get_data(as_text=True))
        self.assertIn("Summary pending", page.get_data(as_text=True))

        extractor.release.set()
        # Use the observed run id rather than accidentally waiting for a new one.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = harness.manager.status()
            if status["run"] and status["run"]["id"] == run_id and not status["active"]:
                break
            time.sleep(0.01)
        else:
            self.fail("pending-summary refresh did not finish")

    def test_04_concurrent_openings_share_one_refresh_and_keep_saved_updates_readable(self) -> None:
        feed_url = "https://slow.example.test/feed.xml"
        slow_client = BlockingFeedClient(feed_url, response(feed_url, rss_feed([])))
        harness = self.make_app(feed_client=slow_client)
        source_id = self.add_source(harness, "Slow source", feed_url)
        harness.database.ingest_entries(
            [
                database_entry(
                    source_id=source_id,
                    source_name="Slow source",
                    url="https://slow.example.test/articles/saved",
                    headline="Already saved update",
                    published_at=utc_hours_ago(1),
                )
            ]
        )

        run_id = harness.manager.start_or_join()
        self.assertTrue(slow_client.started.wait(timeout=3), "slow refresh did not begin")
        with ThreadPoolExecutor(max_workers=6) as pool:
            returned_ids = list(pool.map(lambda _: harness.manager.start_or_join(), range(6)))
        self.assertEqual(returned_ids, [run_id] * 6)
        self.assertEqual(len(slow_client.calls), 0, "feed call is deliberately blocked before it is recorded")

        page = harness.app.test_client().get("/updates?window=7d")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Already saved update", page.get_data(as_text=True))

        slow_client.release.set()
        self.wait_for_run(harness, run_id)
        self.assertEqual(len(slow_client.calls), 1)

    def test_05_failing_feed_does_not_hide_successful_items_or_refresh_its_freshness(self) -> None:
        good_url = "https://good.example.test/feed.xml"
        bad_url = "https://bad.example.test/feed.xml"
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    good_url: response(
                        good_url,
                        rss_feed(
                            [
                                {
                                    "title": "Successful feed item",
                                    "url": "https://good.example.test/articles/one",
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "The successful feed remains visible despite another source failure.",
                                }
                            ]
                        ),
                    ),
                    bad_url: FetchError("Connection failed before a feed could be read"),
                }
            )
        )
        self.add_source(harness, "Good", good_url)
        bad_id = self.add_source(harness, "Bad", bad_url)
        old_success = "2025-01-02T03:04:05Z"
        harness.database.mark_source_success(bad_id, etag="old", last_modified=None, now=old_success)

        self.refresh(harness)

        self.assertEqual([row["headline"] for row in self.query_rows(harness)], ["Successful feed item"])
        bad = harness.database.get_source(bad_id)
        self.assertEqual(bad["last_success_at"], old_success)
        self.assertIsNotNone(bad["last_error"])
        page = harness.app.test_client().get("/updates?window=7d")
        self.assertIn("Successful feed item", page.get_data(as_text=True))
        self.assertIn("Current fetch error", page.get_data(as_text=True))

    def test_06_missing_credentials_and_exhausted_quota_preserve_the_core_feed_experience(self) -> None:
        missing_url = "https://missing-key.example.test/feed.xml"
        missing = self.make_app(
            feed_client=FakeFeedClient(
                {
                    missing_url: response(
                        missing_url,
                        rss_feed(
                            [
                                {
                                    "title": "No credential required for feeds",
                                    "url": "https://missing-key.example.test/articles/one",
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "Feeds remain readable when a model key was not configured on the server.",
                                }
                            ]
                        ),
                    )
                }
            )
        )
        self.add_source(missing, "Missing key", missing_url)
        self.refresh(missing)
        self.assertEqual(self.query_rows(missing)[0]["summary_status"], "pending")
        missing_page = missing.app.test_client().get("/updates?window=7d").get_data(as_text=True)
        self.assertIn("No credential required for feeds", missing_page)
        self.assertIn("server API key is not configured", missing_page)

        quota_url = "https://quota.example.test/feed.xml"
        provider = ValidSummaryProvider()
        quota = self.make_app(
            feed_client=FakeFeedClient(
                {
                    quota_url: response(
                        quota_url,
                        rss_feed(
                            [
                                {
                                    "title": "Visible when quota is exhausted",
                                    "url": "https://quota.example.test/articles/one",
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "This adequate feed excerpt permits a summary attempt without article extraction.",
                                }
                            ]
                        ),
                    )
                }
            ),
            summary_provider=provider,
            groq_api_key="test-key-not-a-real-credential",
            summaries_per_day=0,
        )
        self.add_source(quota, "Quota", quota_url)
        self.refresh(quota)
        quota_row = self.query_rows(quota)[0]
        self.assertEqual(quota_row["summary_status"], "pending")
        self.assertIn("daily free-tier limit", quota_row["summary_reason"])
        self.assertEqual(provider.calls, 0)
        self.assertIn(
            "Visible when quota is exhausted",
            quota.app.test_client().get("/updates?window=7d").get_data(as_text=True),
        )

    def test_07_invalid_summaries_and_fabricated_evidence_are_rejected_without_removing_items(self) -> None:
        paragraph = NumberedParagraph(paragraph_id=1, text="The article says a carefully qualified thing happened today.")
        fabricated = ItemSummary(
            summary="The article reports a qualified development.",
            insufficient_information=False,
            evidence=[EvidenceReference(paragraph_id=1, quote="invented claim")],
        )
        with self.assertRaises(InvalidSummaryError):
            validate_summary(fabricated, [paragraph])

        feed_url = "https://invalid.example.test/feed.xml"
        provider = InvalidEvidenceProvider()
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    feed_url: response(
                        feed_url,
                        rss_feed(
                            [
                                {
                                    "title": "Item survives invalid summary output",
                                    "url": "https://invalid.example.test/articles/one",
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "The feed excerpt is long enough to reach the fake model validation boundary.",
                                }
                            ]
                        ),
                    )
                }
            ),
            summary_provider=provider,
            groq_api_key="test-key-not-a-real-credential",
            model_min_interval_seconds=0,
        )
        self.add_source(harness, "Invalid model", feed_url)

        self.refresh(harness)
        first = self.query_rows(harness)[0]
        self.assertEqual(first["summary_status"], "pending")
        self.assertEqual(first["summary_invalid_attempts"], 1)
        self.assertEqual(first["headline"], "Item survives invalid summary output")

        self.refresh(harness)
        second = self.query_rows(harness)[0]
        self.assertEqual(second["summary_status"], "unavailable")
        self.assertEqual(second["summary_invalid_attempts"], 2)
        self.assertEqual(provider.calls, 2)

    def test_08_changed_feed_metadata_invalidates_the_old_summary(self) -> None:
        feed_url = "https://metadata.example.test/feed.xml"
        harness = self.make_app(feed_client=FakeFeedClient({feed_url: response(feed_url, rss_feed([]))}))
        source_id = self.add_source(harness, "Metadata", feed_url)
        original = database_entry(
            source_id=source_id,
            source_name="Metadata",
            url="https://metadata.example.test/articles/one",
            headline="Original feed title",
            published_at=utc_hours_ago(2),
            excerpt="The original feed excerpt contains enough detail for its first summary.",
        )
        harness.database.ingest_entries([original])
        original_row = self.query_rows(harness)[0]
        harness.database.set_summary_ready(
            item_id=original_row["id"],
            metadata_fingerprint=original.metadata_fingerprint,
            input_fingerprint="input-version-one",
            summary="The original summary belongs to the original metadata version.",
            basis="feed_excerpt",
            evidence=[{"paragraph_id": 1, "quote": "The original feed excerpt"}],
        )

        changed = database_entry(
            source_id=source_id,
            source_name="Metadata",
            url="https://metadata.example.test/articles/one",
            headline="Corrected feed title",
            published_at=utc_hours_ago(1),
            excerpt="The corrected feed excerpt changes the source material used for a summary.",
        )
        counts = harness.database.ingest_entries([changed])
        row = self.query_rows(harness)[0]
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(row["headline"], "Corrected feed title")
        self.assertEqual(row["summary_status"], "pending")
        self.assertIsNone(row["summary_text"])
        self.assertEqual(row["supporting_excerpts"], [])
        self.assertEqual(row["summary_invalid_attempts"], 0)

    def test_09_items_discovered_after_acknowledgement_retain_their_new_indicator(self) -> None:
        feed_url = "https://visit.example.test/feed.xml"
        harness = self.make_app(feed_client=FakeFeedClient({feed_url: response(feed_url, rss_feed([]))}))
        source_id = self.add_source(harness, "Visits", feed_url)
        harness.database.ingest_entries(
            [
                database_entry(
                    source_id=source_id,
                    source_name="Visits",
                    url="https://visit.example.test/articles/first",
                    headline="Acknowledged first item",
                    published_at=utc_hours_ago(1),
                )
            ]
        )

        first_page = harness.app.test_client().get("/updates?window=7d")
        self.assertEqual(first_page.status_code, 200)
        first_sequence = harness.database.acknowledged_sequence()
        self.assertGreater(first_sequence, 0)
        harness.database.ingest_entries(
            [
                database_entry(
                    source_id=source_id,
                    source_name="Visits",
                    url="https://visit.example.test/articles/second",
                    headline="Arrived after acknowledgement",
                    published_at=utc_hours_ago(0),
                )
            ]
        )

        before_second_render = harness.database.query_updates(
            cutoff=utc_hours_ago(24 * 8),
            source_id=None,
            search=None,
            acknowledged_sequence=first_sequence,
        )
        self.assertEqual(
            {row["headline"] for row in before_second_render if row["is_new"]},
            {"Arrived after acknowledgement"},
        )
        second_page = harness.app.test_client().get("/updates?window=7d").get_data(as_text=True)
        self.assertIn("Arrived after acknowledgement", second_page)
        self.assertIn('new-badge">New', second_page)

    def test_10_source_filters_disabled_sources_and_discovery_timestamp_fallbacks(self) -> None:
        alpha_url = "https://filter-alpha.example.test/feed.xml"
        beta_url = "https://filter-beta.example.test/feed.xml"
        disabled_url = "https://filter-disabled.example.test/feed.xml"
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    alpha_url: response(alpha_url, rss_feed([])),
                    beta_url: response(beta_url, rss_feed([])),
                    disabled_url: response(disabled_url, rss_feed([])),
                }
            )
        )
        alpha_id = self.add_source(harness, "Alpha filter", alpha_url)
        beta_id = self.add_source(harness, "Beta filter", beta_url)
        disabled_id = self.add_source(harness, "Disabled filter", disabled_url, enabled=False)
        harness.database.ingest_entries(
            [
                database_entry(
                    source_id=alpha_id,
                    source_name="Alpha filter",
                    url="https://filter-alpha.example.test/articles/needle",
                    headline="Needle from alpha",
                    published_at=utc_hours_ago(2),
                ),
                database_entry(
                    source_id=beta_id,
                    source_name="Beta filter",
                    url="https://filter-beta.example.test/articles/no-date",
                    headline="Beta without publication date",
                    published_at=None,
                ),
                database_entry(
                    source_id=disabled_id,
                    source_name="Disabled filter",
                    url="https://filter-disabled.example.test/articles/needle",
                    headline="Needle from disabled source",
                    published_at=utc_hours_ago(1),
                ),
            ]
        )

        default_rows = self.query_rows(harness)
        self.assertEqual({row["headline"] for row in default_rows}, {"Needle from alpha", "Beta without publication date"})
        fallback = next(row for row in default_rows if row["headline"] == "Beta without publication date")
        self.assertEqual(fallback["timestamp_is_discovered"], 1)
        filtered_page = harness.app.test_client().get(
            f"/updates?window=7d&source={alpha_id}&q=Needle"
        ).get_data(as_text=True)
        self.assertIn("Needle from alpha", filtered_page)
        self.assertNotIn("Beta without publication date", filtered_page)
        self.assertNotIn("Needle from disabled source", filtered_page)
        all_page = harness.app.test_client().get("/updates?window=7d").get_data(as_text=True)
        self.assertIn("Discovered", all_page)
        self.assertNotIn("Needle from disabled source", all_page)

    def test_11_unsafe_urls_access_restrictions_and_oversized_responses_are_handled(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            validate_source_url("http://127.0.0.1/private-feed.xml")
        with self.assertRaises(UnsafeUrlError):
            validate_source_url("http://[::1]/private-feed.xml")

        class OversizedResponse:
            def iter_content(self, chunk_size: int) -> Any:
                yield b"x" * 8
                yield b"y" * 8

        with self.assertRaises(ResponseTooLargeError):
            _read_bounded_body(OversizedResponse(), 12)

        class RestrictedClient:
            def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
                if url.endswith("/robots.txt"):
                    return response(url, b"", status_code=404)
                return response(url, b"", status_code=403)

        with self.assertRaises(AccessRestrictedError):
            ArticleExtractor(RestrictedClient()).extract("https://publisher.example.test/article")

        feed_url = "https://restricted.example.test/feed.xml"
        article_url = "https://restricted.example.test/articles/one"
        provider = ValidSummaryProvider()
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    feed_url: response(
                        feed_url,
                        rss_feed(
                            [
                                {
                                    "title": "Visible despite article restriction",
                                    "url": article_url,
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "A sufficiently detailed feed excerpt is available when article access is restricted.",
                                }
                            ]
                        ),
                    )
                }
            ),
            article_extractor=FakeArticleExtractor({article_url: AccessRestrictedError("publisher restricted access")}),
            summary_provider=provider,
            groq_api_key="test-key-not-a-real-credential",
        )
        self.add_source(harness, "Restricted", feed_url, extract_articles=True)
        self.refresh(harness)
        row = self.query_rows(harness)[0]
        self.assertEqual(row["summary_status"], "ready")
        self.assertEqual(row["summary_basis"], "feed_excerpt")
        self.assertEqual(provider.calls, 1)

    def test_12_restart_backup_and_restore_preserve_configuration_items_visit_state_and_usage(self) -> None:
        feed_url = "https://recovery.example.test/feed.xml"
        harness = self.make_app(feed_client=FakeFeedClient({feed_url: response(feed_url, rss_feed([]))}))
        source_id = self.add_source(harness, "Recovery", feed_url)
        harness.database.ingest_entries(
            [
                database_entry(
                    source_id=source_id,
                    source_name="Recovery",
                    url="https://recovery.example.test/articles/one",
                    headline="Recoverable item",
                    published_at=utc_hours_ago(1),
                )
            ]
        )
        item = self.query_rows(harness)[0]
        harness.database.acknowledge_through(item["discovery_sequence"])
        run_id = harness.database.create_refresh_run(1)
        attempt_id, limited_reason = harness.database.reserve_model_attempt(
            refresh_run_id=run_id,
            item_id=item["id"],
            input_fingerprint="recovery-input",
            daily_limit=30,
            minimum_interval_seconds=0,
        )
        self.assertIsNotNone(attempt_id)
        self.assertIsNone(limited_reason)
        harness.database.complete_model_attempt(attempt_id, outcome="ready")

        restarted = create_app(
            harness.settings,
            feed_client=FakeFeedClient({feed_url: response(feed_url, rss_feed([]))}),
            article_extractor=FakeArticleExtractor(),
        )
        restarted_database = restarted.extensions["pykasu_database"]
        self.assertEqual(restarted_database.get_source(source_id)["name"], "Recovery")
        self.assertEqual(restarted_database.acknowledged_sequence(), item["discovery_sequence"])
        self.assertEqual(restarted_database.model_usage()["attempt_count"], 1)

        with tempfile.TemporaryDirectory() as directory:
            backup_path = Path(directory) / "pykasu-backup.sqlite3"
            restored_path = Path(directory) / "restored" / "pykasu.sqlite3"
            sqlite_backup(harness.settings.database_path, backup_path)
            restore_backup(backup_path, restored_path)
            restored = Database(restored_path)
            restored.initialize()
            restored_rows = restored.query_updates(
                cutoff=utc_hours_ago(24 * 8), source_id=None, search=None, acknowledged_sequence=0
            )
            self.assertEqual([row["headline"] for row in restored_rows], ["Recoverable item"])
            self.assertEqual(restored.get_source(source_id)["feed_url"], feed_url)
            self.assertEqual(restored.acknowledged_sequence(), item["discovery_sequence"])
            self.assertEqual(restored.model_usage()["attempt_count"], 1)

    def test_13_persistence_excludes_full_article_bodies_prompts_and_credentials(self) -> None:
        feed_url = "https://privacy.example.test/feed.xml"
        article_url = "https://privacy.example.test/articles/one"
        full_body_marker = "FULL_ARTICLE_BODY_MUST_NEVER_REACH_SQLITE"
        prompt_marker = "UNTRUSTED_PROMPT_TEXT_MUST_NEVER_REACH_SQLITE"
        api_key_marker = "test-api-key-must-never-reach-sqlite"
        article_text = (
            "Officials said the decision remains subject to review while the source reports further details. "
            + "ordinary context " * 30
            + full_body_marker
            + " "
            + prompt_marker
            + " "
            + "trailing material makes this an intentionally full article body rather than an excerpt."
        )
        harness = self.make_app(
            feed_client=FakeFeedClient(
                {
                    feed_url: response(
                        feed_url,
                        rss_feed(
                            [
                                {
                                    "title": "Private processing fixture",
                                    "url": article_url,
                                    "published": recent_rfc2822(hours_ago=1),
                                    "excerpt": "A short feed excerpt remains the only source text allowed in durable storage.",
                                }
                            ]
                        ),
                    )
                }
            ),
            article_extractor=FakeArticleExtractor({article_url: article_text}),
            summary_provider=ValidSummaryProvider(),
            groq_api_key=api_key_marker,
        )
        self.add_source(harness, "Privacy", feed_url, extract_articles=True)
        self.refresh(harness)
        self.assertEqual(self.query_rows(harness)[0]["summary_status"], "ready")

        with harness.database.connection() as conn:
            dump = "\n".join(conn.iterdump())
        for forbidden in (full_body_marker, prompt_marker, api_key_marker):
            self.assertNotIn(forbidden, dump)
        for candidate in harness.settings.data_dir.glob("pykasu.sqlite3*"):
            raw = candidate.read_bytes()
            for forbidden in (full_body_marker, prompt_marker, api_key_marker):
                self.assertNotIn(forbidden.encode("utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
