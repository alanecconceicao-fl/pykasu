"""Data structures passed between the deterministic processing layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class FeedEntry:
    source_id: int
    source_name: str
    url: str
    normalized_url: str
    headline: str
    published_at: str | None
    excerpt: str
    metadata_fingerprint: str


@dataclass(frozen=True)
class NumberedParagraph:
    paragraph_id: int
    text: str
