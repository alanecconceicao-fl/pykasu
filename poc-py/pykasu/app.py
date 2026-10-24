"""Flask application factory and the two browser-facing pages."""

from __future__ import annotations

import sqlite3
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, url_for

from .config import Settings
from .db import Database, EnabledSourceLimitError
from .http_client import ArticleExtractor, SafeHttpClient, UnsafeUrlError, validate_source_url
from .refresh import RefreshManager
from .summaries import GroqSummaryClient, SummaryProvider
from .util import as_utc_text, normalize_text, parse_utc, utc_now


def create_app(
    settings: Settings | None = None,
    *,
    feed_client: Any | None = None,
    article_extractor: Any | None = None,
    summary_provider: SummaryProvider | None = None,
) -> Flask:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    http_client = feed_client or SafeHttpClient(settings)
    extractor = article_extractor or ArticleExtractor(http_client)
    provider = summary_provider
    if provider is None and settings.groq_api_key and settings.groq_zero_data_retention:
        provider = GroqSummaryClient(settings)
    manager = RefreshManager(
        database=database,
        settings=settings,
        feed_client=http_client,
        article_extractor=extractor,
        summary_provider=provider,
    )

    public_static = Path(__file__).resolve().parent.parent / "public" / "static"
    app = Flask(__name__, static_folder=str(public_static), static_url_path="/static")
    # The public PoC has no login, but Flask flashes still need a per-instance
    # signing key. It does not protect API credentials and is never displayed.
    app.config.update(SECRET_KEY="pykasu-local-session-key")
    app.extensions["pykasu_database"] = database
    app.extensions["pykasu_settings"] = settings
    app.extensions["pykasu_refresh_manager"] = manager

    @app.context_processor
    def template_context() -> dict[str, object]:
        return {
            "has_groq_key": bool(settings.groq_api_key),
            "summary_service_enabled": provider is not None,
        }

    @app.template_filter("timestamp")
    def timestamp(value: str | None) -> str:
        parsed = parse_utc(value)
        return parsed.strftime("%d %b %Y, %H:%M UTC") if parsed else "Unknown time"

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none';",
        )
        # Every top-level request must reach Flask to start/join a refresh, and
        # polling/fragment responses must reflect current worker progress.
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        response.headers.setdefault("Pragma", "no-cache")
        return response

    @app.get("/")
    def home() -> Response:
        return redirect(url_for("updates"))

    @app.get("/updates")
    def updates() -> str:
        view = _updates_view(database)
        # Query and acknowledge saved rows first. If a worker commits anything
        # after this point, its higher discovery sequence remains New.
        manager.start_or_join()
        return render_template(
            "updates.html",
            **view,
            source_health=database.list_sources(enabled_only=True),
            initial_refresh=manager.status(),
        )

    @app.get("/updates/fragment")
    def updates_fragment() -> Response:
        """Refresh just the rendered list; importantly, this never starts work."""

        view = _updates_view(database)
        html = render_template("_items.html", items=view["items"])
        return jsonify({"html": html, "max_sequence": view["delivered_sequence"]})

    @app.get("/sources")
    def sources() -> str:
        sources = database.list_sources()
        # A source configuration page is a top-level load too, but it does not
        # acknowledge item sequences because it did not deliver any items.
        manager.start_or_join()
        return render_template(
            "sources.html",
            sources=sources,
            enabled_count=database.count_enabled_sources(),
            max_enabled_sources=settings.max_enabled_sources,
            initial_refresh=manager.status(),
        )

    @app.post("/sources")
    def create_source() -> Response:
        values, error = _source_form_values(database, settings)
        if error:
            flash(error, "error")
            return redirect(url_for("sources"))
        assert values is not None
        if values[2] and database.count_enabled_sources() >= settings.max_enabled_sources:
            flash(f"At most {settings.max_enabled_sources} sources can be enabled.", "error")
            return redirect(url_for("sources"))
        try:
            database.create_source(*values, maximum_enabled=settings.max_enabled_sources)
        except EnabledSourceLimitError:
            flash(f"At most {settings.max_enabled_sources} sources can be enabled.", "error")
        except sqlite3.IntegrityError:
            flash("That feed URL is already configured.", "error")
        else:
            flash("Source saved. It will be checked on the next page load.", "success")
        return redirect(url_for("sources"))

    @app.post("/sources/<int:source_id>")
    def edit_source(source_id: int) -> Response:
        old = database.get_source(source_id)
        if old is None:
            abort(404)
        values, error = _source_form_values(database, settings)
        if error:
            flash(error, "error")
            return redirect(url_for("sources"))
        assert values is not None
        if values[2] and not old["enabled"] and database.count_enabled_sources() >= settings.max_enabled_sources:
            flash(f"At most {settings.max_enabled_sources} sources can be enabled.", "error")
            return redirect(url_for("sources"))
        try:
            database.update_source(source_id, *values, maximum_enabled=settings.max_enabled_sources)
        except EnabledSourceLimitError:
            flash(f"At most {settings.max_enabled_sources} sources can be enabled.", "error")
        except sqlite3.IntegrityError:
            flash("That feed URL is already configured.", "error")
        else:
            flash("Source updated.", "success")
        return redirect(url_for("sources"))

    @app.get("/refresh-status")
    def refresh_status() -> Response:
        return jsonify(manager.status())

    base_path = os.environ.get("PYKASU_BASE_PATH", "").rstrip("/")
    if base_path:
        app.wsgi_app = PrefixPathMiddleware(app.wsgi_app, base_path)
    return app


class PrefixPathMiddleware:
    """Mount Flask below a public path prefix while preserving url_for links."""

    def __init__(self, application: Any, prefix: str) -> None:
        self.application = application
        self.prefix = "/" + prefix.strip("/")

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        path_info = environ.get("PATH_INFO", "")
        script_name = environ.get("SCRIPT_NAME", "").rstrip("/")
        if path_info == self.prefix or path_info.startswith(self.prefix + "/"):
            if not script_name.endswith(self.prefix):
                environ["SCRIPT_NAME"] = script_name + self.prefix
            environ["PATH_INFO"] = path_info[len(self.prefix):] or "/"
        return self.application(environ, start_response)


def _source_form_values(
    database: Database, settings: Settings
) -> tuple[tuple[str, str, bool, bool] | None, str | None]:
    name = normalize_text(request.form.get("name"))
    raw_url = normalize_text(request.form.get("feed_url"))
    if not name or len(name) > 120:
        return None, "Give the source a name of up to 120 characters."
    try:
        feed_url = validate_source_url(raw_url)
    except (ValueError, UnsafeUrlError):
        return None, "Use a public http(s) RSS or Atom URL."
    return (
        name,
        feed_url,
        request.form.get("enabled") == "on",
        request.form.get("extract_articles") == "on",
    ), None


def _updates_view(database: Database) -> dict[str, object]:
    window = request.args.get("window", "24h")
    if window not in {"24h", "7d"}:
        window = "24h"
    hours = 24 if window == "24h" else 24 * 7
    source_id = _selected_enabled_source_id(database, request.args.get("source"))
    search = normalize_text(request.args.get("q"))[:160]
    previous_acknowledgement = database.acknowledged_sequence()
    cutoff = as_utc_text(utc_now() - timedelta(hours=hours))
    items = database.query_updates(
        cutoff=cutoff,
        source_id=source_id,
        search=search or None,
        acknowledged_sequence=previous_acknowledgement,
    )
    delivered_sequence = max((int(item["discovery_sequence"]) for item in items), default=0)
    database.acknowledge_through(delivered_sequence)
    return {
        "items": items,
        "sources": database.list_sources(enabled_only=True),
        "selected_source_id": source_id,
        "selected_window": window,
        "search": search,
        "previous_acknowledgement": previous_acknowledgement,
        "delivered_sequence": delivered_sequence,
    }


def _selected_enabled_source_id(database: Database, value: str | None) -> int | None:
    if not value:
        return None
    try:
        source_id = int(value)
    except ValueError:
        return None
    source = database.get_source(source_id)
    return source_id if source and source["enabled"] else None
