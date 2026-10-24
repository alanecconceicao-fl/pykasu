"""Small, dependency-free helpers shared across the application."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc_text(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


_whitespace = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """Normalize text for comparisons, storage excerpts, and UI labels."""

    return _whitespace.sub(" ", html.unescape(value or "")).strip()


class _TextOnlyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Turn feed-provided HTML into a short plain-text excerpt, never markup."""

    parser = _TextOnlyParser()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        # Feed metadata is untrusted. A plain normalized fallback is safer than
        # propagating malformed markup into templates or a model prompt.
        return normalize_text(value)
    return normalize_text(" ".join(parser.parts))


def limited_excerpt(value: str | None, maximum: int = 1_200) -> str:
    text = normalize_text(value)
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_url(value: str) -> str:
    """Canonicalize only URL details that cannot change the resource identity.

    Query parameters intentionally remain untouched. Removing campaign-like
    parameters would be guessing, and can accidentally merge distinct items.
    """

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A source item URL must use http or https and include a host")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("The URL host is invalid") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The URL port is invalid") from exc
    scheme = parsed.scheme.lower()
    # urlsplit().hostname intentionally removes IPv6 brackets; urlunsplit()
    # requires them back or the normalized URL becomes unparsable and can
    # evade the literal-address safety check.
    is_ipv6 = ":" in host
    netloc = f"[{host}]" if is_ipv6 else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"[{host}]:{port}" if is_ipv6 else f"{host}:{port}"
    path = parsed.path or "/"
    cleaned = SplitResult(scheme, netloc, path, parsed.query, "")
    return urlunsplit(cleaned)


def safe_error(value: object, maximum: int = 180) -> str:
    """Produce a UI-safe operational error without retaining response bodies."""

    text = normalize_text(str(value))
    # Credentials should never be reflected in a source status or log output.
    text = re.sub(r"(?i)(authorization|api[_ -]?key|bearer)\s*[:=]?\s*\S+", r"\1 [redacted]", text)
    if not text:
        text = "Request failed"
    return text[:maximum]
