"""Single-flight background refresh: feed visibility first, enrichment later."""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol, Sequence

from .config import Settings
from .db import Database
from .feeds import FeedParseError, conditional_headers, parse_feed_entries
from .http_client import FetchError
from .models import FetchResponse, NumberedParagraph
from .summaries import (
    InvalidSummaryError,
    ModelQuotaError,
    ModelRequestError,
    SummaryProvider,
    feed_excerpt_is_usable,
    paragraphs_from_text,
    prompt_fits_limit,
    storage_evidence,
    summary_for_item,
    summary_input_fingerprint,
)
from .util import parse_utc, safe_error, utc_now


class FeedClient(Protocol):
    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchResponse:
        ...


class Extractor(Protocol):
    def extract(self, url: str) -> str:
        ...


class RefreshManager:
    """Owns at most one worker thread for a process.

    Saved results are queried by the web thread before it calls ``start_or_join``.
    The worker uses new SQLite connections through ``Database`` and holds no
    database transaction while doing feed, robots, article, or model I/O.
    """

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        feed_client: FeedClient,
        article_extractor: Extractor,
        summary_provider: SummaryProvider | None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.feed_client = feed_client
        self.article_extractor = article_extractor
        self.summary_provider = summary_provider
        self._lock = threading.Lock()
        self._active_thread: threading.Thread | None = None
        self._active_run_id: int | None = None

    def start_or_join(self) -> int:
        with self._lock:
            if self._active_thread is not None and self._active_thread.is_alive() and self._active_run_id is not None:
                return self._active_run_id
            sources = self.database.list_sources(enabled_only=True)
            run_id = self.database.create_refresh_run(len(sources))
            worker = threading.Thread(
                target=self._run,
                args=(run_id, sources),
                daemon=True,
                name=f"pykasu-refresh-{run_id}",
            )
            self._active_thread = worker
            self._active_run_id = run_id
            worker.start()
            return run_id

    def status(self) -> dict[str, Any]:
        run = self.database.latest_refresh_run()
        with self._lock:
            active = bool(self._active_thread and self._active_thread.is_alive())
            active_run_id = self._active_run_id if active else None
        return {
            "active": active,
            "active_run_id": active_run_id,
            "run": run,
            "max_discovery_sequence": self.database.current_max_discovery_sequence(),
        }

    def _run(self, run_id: int, sources: Sequence[dict[str, Any]]) -> None:
        try:
            for source in sources:
                self._refresh_source(run_id, source)
            self.database.update_refresh_run(run_id, phase="summarizing")
            completed = self._summarize_candidates(run_id)
            if completed:
                self.database.update_refresh_run(run_id, summaries_completed_increment=completed)
            self.database.finish_refresh_run(run_id)
        except BaseException:
            # Do not put exception representations into logs/status: network
            # libraries may include bodies or headers. The durable run remains
            # useful without disclosing source content or credentials.
            self.database.finish_refresh_run(run_id, failed=True, message="Refresh worker stopped unexpectedly")
        finally:
            with self._lock:
                if self._active_run_id == run_id:
                    self._active_thread = None
                    self._active_run_id = None

    def _refresh_source(self, run_id: int, source: dict[str, Any]) -> None:
        source_id = int(source["id"])
        try:
            response = self.feed_client.fetch(str(source["feed_url"]), conditional_headers(source))
            if response.status_code == 304:
                saved = self.database.mark_source_success(
                    source_id,
                    etag=_header(response.headers, "ETag"),
                    last_modified=_header(response.headers, "Last-Modified"),
                    expected_feed_url=str(source["feed_url"]),
                    expected_revision=int(source["revision"]),
                )
                self.database.record_refresh_source_result(
                    run_id,
                    source_id,
                    "not_modified" if saved else "failed",
                    error=None if saved else "Source configuration changed during refresh; result discarded.",
                )
            elif response.status_code == 200:
                entries = parse_feed_entries(source, response.body, self.settings.entries_per_feed)
                # This commit happens before any article extraction/model call.
                counts = self.database.ingest_entries(
                    entries,
                    expected_source_id=source_id,
                    expected_feed_url=str(source["feed_url"]),
                    expected_revision=int(source["revision"]),
                )
                saved = self.database.mark_source_success(
                    source_id,
                    etag=_header(response.headers, "ETag"),
                    last_modified=_header(response.headers, "Last-Modified"),
                    expected_feed_url=str(source["feed_url"]),
                    expected_revision=int(source["revision"]),
                )
                if counts.get("discarded") or not saved:
                    self.database.record_refresh_source_result(
                        run_id,
                        source_id,
                        "failed",
                        error="Source configuration changed during refresh; result discarded.",
                    )
                else:
                    self.database.record_refresh_source_result(
                        run_id, source_id, "success", item_count=len(entries)
                    )
                    self.database.update_refresh_run(
                        run_id,
                        items_inserted_increment=int(counts["inserted"]),
                        items_updated_increment=int(counts["updated"]),
                    )
            else:
                raise FetchError(f"Feed request returned HTTP {response.status_code}")
        except (FetchError, FeedParseError, ValueError) as exc:
            error = safe_error(exc)
            self.database.mark_source_failure(
                source_id,
                error,
                expected_feed_url=str(source["feed_url"]),
                expected_revision=int(source["revision"]),
            )
            self.database.record_refresh_source_result(run_id, source_id, "failed", error=error)
        except Exception:
            # A feed parser or injected client must not prevent other feeds from
            # appearing, and its raw exception must not leak feed response data.
            error = "Feed processing failed"
            self.database.mark_source_failure(
                source_id,
                error,
                expected_feed_url=str(source["feed_url"]),
                expected_revision=int(source["revision"]),
            )
            self.database.record_refresh_source_result(run_id, source_id, "failed", error=error)
        finally:
            self.database.update_refresh_run(run_id, source_completed_increment=1)

    def _summarize_candidates(self, run_id: int) -> int:
        if self.summary_provider is None:
            return 0
        completed = 0
        candidates = self.database.summary_candidates(self.settings.summaries_per_refresh)
        for item in candidates:
            item_id = int(item["id"])
            metadata_fingerprint = str(item["metadata_fingerprint"])
            if not self.database.mark_summary_processing(item_id, metadata_fingerprint):
                continue
            try:
                basis, paragraphs = self._summary_input(item)
                if not paragraphs:
                    self.database.set_summary_unavailable(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        reason="No usable article text or feed excerpt was available.",
                    )
                    continue
                metadata = {
                    "headline": str(item["headline"]),
                    "source_names": str(item.get("source_names") or ""),
                    "published_at": str(item.get("published_at") or ""),
                }
                input_fingerprint = summary_input_fingerprint(metadata, paragraphs)
                if not prompt_fits_limit(metadata, paragraphs, self.settings):
                    # This is a local processing boundary, not an invalid
                    # provider response. It must not consume the free daily
                    # quota or count toward the two-invalid-output rule.
                    self.database.set_summary_unavailable(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        input_fingerprint=input_fingerprint,
                        reason="Source text could not fit the safe summary input limit.",
                    )
                    continue
                attempt_id, limited_reason = self._reserve_model_attempt_when_allowed(
                    run_id=run_id,
                    item_id=item_id,
                    input_fingerprint=input_fingerprint,
                )
                if attempt_id is None:
                    self.database.return_summary_to_pending(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        reason=(
                            "Summary pending: the daily free-tier limit has been reached."
                            if limited_reason == "daily_limit"
                            else "Summary pending: the provider cooldown is active."
                        ),
                    )
                    # The global cooldown makes later candidates in this run
                    # ineligible as well; a later top-level visit can resume.
                    break
                try:
                    result = summary_for_item(metadata, paragraphs, provider=self.summary_provider)
                except ModelQuotaError as exc:
                    self.database.complete_model_attempt(
                        attempt_id,
                        outcome="quota_exhausted",
                        failure_reason="Provider quota exhausted",
                        retry_after_at=exc.retry_after_at,
                    )
                    self.database.set_model_cooldown(exc.retry_after_at)
                    self.database.return_summary_to_pending(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        reason="Summary pending: the provider asked us to retry later.",
                    )
                    break
                except InvalidSummaryError as exc:
                    self.database.complete_model_attempt(
                        attempt_id, outcome="invalid", failure_reason=safe_error(exc)
                    )
                    invalid_count = self.database.record_invalid_summary(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        input_fingerprint=input_fingerprint,
                        reason="Summary unavailable: the provider response could not be validated.",
                    )
                    if invalid_count >= 2:
                        completed += 1
                    continue
                except ModelRequestError:
                    self.database.complete_model_attempt(
                        attempt_id, outcome="failed", failure_reason="Model request failed"
                    )
                    self.database.return_summary_to_pending(
                        item_id=item_id,
                        metadata_fingerprint=metadata_fingerprint,
                        reason="Summary pending: the provider is temporarily unavailable.",
                    )
                    break
                else:
                    if result.insufficient_information:
                        self.database.complete_model_attempt(attempt_id, outcome="insufficient")
                        self.database.set_summary_unavailable(
                            item_id=item_id,
                            metadata_fingerprint=metadata_fingerprint,
                            input_fingerprint=input_fingerprint,
                            reason="The available source text was insufficient for a summary.",
                        )
                    else:
                        self.database.complete_model_attempt(attempt_id, outcome="ready")
                        self.database.set_summary_ready(
                            item_id=item_id,
                            metadata_fingerprint=metadata_fingerprint,
                            input_fingerprint=input_fingerprint,
                            summary=result.summary,
                            basis=basis,
                            evidence=storage_evidence(result),
                        )
                    completed += 1
                    continue
            except Exception:
                # Ensure a transient extractor defect cannot strand an item in
                # processing or hide the saved chronological feed.
                self.database.return_summary_to_pending(
                    item_id=item_id,
                    metadata_fingerprint=metadata_fingerprint,
                    reason="Summary pending: source processing will retry later.",
                )
                break
        return completed

    def _reserve_model_attempt_when_allowed(
        self, *, run_id: int, item_id: int, input_fingerprint: str
    ) -> tuple[int | None, str | None]:
        """Pace up to ten attempts in one active refresh without a scheduler.

        A normal 65-second interval is waited for in the worker, never in a
        request thread. Provider quota cooldowns (normally much longer) end
        this run and are retried only after a later top-level page access.
        """

        while True:
            attempt_id, reason = self.database.reserve_model_attempt(
                refresh_run_id=run_id,
                item_id=item_id,
                input_fingerprint=input_fingerprint,
                daily_limit=self.settings.summaries_per_day,
                minimum_interval_seconds=self.settings.model_min_interval_seconds,
            )
            if attempt_id is not None or reason != "cooldown":
                return attempt_id, reason
            cooldown = parse_utc(self.database.model_usage().get("cooldown_until"))
            seconds = (cooldown - utc_now()).total_seconds() if cooldown else 0
            # A longer deadline comes from a provider quota response, not the
            # regular spacing rule. Do not turn this one worker into a queue.
            if seconds > self.settings.model_min_interval_seconds + 1:
                return None, "cooldown"
            if seconds <= 0:
                continue
            # Short increments leave the process responsive to shutdown and do
            # not make a request-serving thread wait on model pacing.
            time.sleep(min(seconds, 1.0))

    def _summary_input(self, item: dict[str, Any]) -> tuple[str, list[NumberedParagraph]]:
        if item.get("may_extract"):
            try:
                article_text = self.article_extractor.extract(str(item["url"]))
                paragraphs = paragraphs_from_text(article_text)
                if paragraphs:
                    return "article", paragraphs
            except (FetchError, ValueError):
                # A short source-provided excerpt can still support a clearly
                # labeled summary. The failed body itself remains transient.
                pass
            except Exception:
                pass
        excerpt = str(item.get("feed_excerpt") or "")
        if feed_excerpt_is_usable(str(item["headline"]), excerpt):
            return "feed_excerpt", paragraphs_from_text(excerpt, character_budget=1_200)
        return "", []

def _header(headers: Any, name: str) -> str | None:
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None
