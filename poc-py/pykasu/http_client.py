"""Bounded HTTP fetching with conservative SSRF and access protections."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from urllib3.exceptions import HTTPError as Urllib3HttpError
from urllib3.util import Timeout as Urllib3Timeout

from .config import Settings
from .models import FetchResponse
from .util import as_utc_text, normalize_url, utc_now


class FetchError(RuntimeError):
    """A safe, body-free explanation suitable for a source status."""


class UnsafeUrlError(FetchError):
    pass


class AccessRestrictedError(FetchError):
    pass


class ResponseTooLargeError(FetchError):
    pass


# ``urllib3`` otherwise resolves the hostname a second time internally, before
# its connection timeout starts. These locks make that internal lookup return
# only the already-validated records for the short request-setup window.
_DNS_PATCH_LOCK = threading.RLock()
_DNS_SLOTS = threading.BoundedSemaphore(2)


def validate_public_url(url: str, *, deadline: float | None = None) -> str:
    """Validate a URL before every network hop, including redirects.

    DNS is resolved immediately before requests opens a connection. Every
    returned address must be public; accepting a mixed public/private response
    would leave a DNS-rebinding path open.
    """

    normalized, _ = _validated_destination(url, deadline=deadline)
    return normalized


def _validated_destination(
    url: str, *, deadline: float | None
) -> tuple[str, list[tuple[object, ...]]]:
    """Normalize a URL and return its already-checked connection records."""

    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("Localhost destinations are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _require_public_address(literal)
        records = _resolve_host(host, parsed.port or (443 if parsed.scheme == "https" else 80), deadline=deadline)
        return normalized, records
    records = _resolve_host(
        host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        deadline=deadline,
    )
    if not records:
        raise FetchError("The host did not resolve to an address")
    addresses = {record[4][0] for record in records}
    for address in addresses:
        try:
            _require_public_address(ipaddress.ip_address(address))
        except ValueError as exc:
            raise UnsafeUrlError("The host resolved to an invalid address") from exc
    return normalized, records


@contextmanager
def pinned_public_destination(url: str, *, deadline: float) -> Iterator[str]:
    """Pin urllib3's target resolver to prevalidated records for one request."""

    normalized, records = _validated_destination(url, deadline=deadline)
    parsed = urlsplit(normalized)
    with _pin_resolution(
        parsed.hostname or "",
        parsed.port or (443 if parsed.scheme == "https" else 80),
        records,
        deadline=deadline,
    ):
        yield normalized


def _resolve_host(host: str, port: int, *, deadline: float | None) -> list[tuple[object, ...]]:
    """Resolve DNS without letting a slow resolver exceed a request deadline."""

    if deadline is None:
        try:
            return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise FetchError("The host could not be resolved") from exc

    result: dict[str, object] = {}
    done = threading.Event()

    def lookup() -> None:
        try:
            result["records"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # Captured only to return a safe error.
            result["error"] = exc
        finally:
            _DNS_SLOTS.release()
            done.set()

    # Do not let a resolver race with the temporary pinned-resolver patch used
    # by requests/urllib3 below. At most two daemon resolver calls can outlive
    # their deadline, preventing unbounded stuck-thread accumulation. Waiting
    # for the global pin is itself bounded by this request's deadline.
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _DNS_PATCH_LOCK.acquire(timeout=remaining):
        raise FetchError("DNS resolution exceeded the request deadline")
    try:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise FetchError("DNS resolver capacity is temporarily unavailable")
        try:
            worker = threading.Thread(target=lookup, daemon=True, name="pykasu-dns")
            worker.start()
        except BaseException as exc:
            _DNS_SLOTS.release()
            raise FetchError("DNS resolver could not be started") from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not done.wait(remaining):
            # The daemon owns the slot until its system resolver returns.
            # This caps abandoned calls without pretending cancellation works.
            raise FetchError("DNS resolution exceeded the request deadline")
    finally:
        _DNS_PATCH_LOCK.release()
    if "error" in result:
        raise FetchError("The host could not be resolved")
    records = result.get("records")
    return list(records) if isinstance(records, list) else []


@contextmanager
def _pin_resolution(
    host: str,
    port: int,
    records: list[tuple[object, ...]],
    *,
    deadline: float | None = None,
) -> Iterator[None]:
    """Temporarily prevent urllib3 from making an unbounded second DNS call."""

    expected_host = host.rstrip(".").casefold()

    def pinned_getaddrinfo(
        requested_host: str,
        requested_port: int | str | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[object, ...]]:
        requested = str(requested_host).strip("[]").rstrip(".").casefold()
        # urllib3 normally supplies an integer port, but accepting its common
        # string forms avoids accidentally falling through to a fresh lookup.
        if isinstance(requested_port, str) and requested_port.casefold() == "https":
            requested_port_number: int | None = 443
        elif isinstance(requested_port, str) and requested_port.casefold() == "http":
            requested_port_number = 80
        else:
            try:
                requested_port_number = int(requested_port) if requested_port is not None else None
            except (TypeError, ValueError):
                requested_port_number = None
        if requested == expected_host and requested_port_number == port:
            selected = [
                record
                for record in records
                if (family in (0, socket.AF_UNSPEC) or record[0] == family)
                and (type == 0 or record[1] == type)
                and (proto == 0 or record[2] == proto)
            ]
            if selected:
                return selected
            raise socket.gaierror("No validated address matches this connection")
        return original(requested_host, requested_port, family, type, proto, flags)

    if deadline is None:
        _DNS_PATCH_LOCK.acquire()
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _DNS_PATCH_LOCK.acquire(timeout=remaining):
            raise FetchError("Request deadline exceeded before connection setup")
    # Capture the resolver only after serialization. Capturing it before the
    # lock would allow two overlapping contexts to restore each other's
    # wrapper instead of the real resolver on exit.
    original = socket.getaddrinfo
    try:
        socket.getaddrinfo = pinned_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original
    finally:
        _DNS_PATCH_LOCK.release()


def validate_source_url(url: str) -> str:
    """Cheap source-form validation that does not require a DNS lookup."""

    normalized = normalize_url(url)
    host = urlsplit(normalized).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("Localhost destinations are not allowed")
    try:
        _require_public_address(ipaddress.ip_address(host))
    except ValueError:
        # A DNS name is revalidated and resolved just before every fetch.
        pass
    return normalized


def _require_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    # is_global deliberately rejects loopback, private, link-local, multicast,
    # documentation, reserved, carrier-grade NAT, and unspecified ranges.
    if not address.is_global:
        raise UnsafeUrlError("Private, local, or reserved network destinations are not allowed")


class SafeHttpClient:
    """Fetch bytes without following an unvalidated redirect or retaining data."""

    def __init__(self, settings: Settings, *, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        # Environment proxy settings could turn an outward URL into a local
        # connection. This PoC makes direct, validated connections instead.
        self.session.trust_env = False

    def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        allow_url: Callable[[str], bool] | None = None,
    ) -> FetchResponse:
        deadline = time.monotonic() + self.settings.request_deadline_seconds
        current_url = url
        request_headers = {
            "User-Agent": "PYKASU-PoC/0.1 (+source-item-reader)",
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.1",
        }
        if headers:
            request_headers.update(headers)
        for redirect_count in range(self.settings.max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("Request deadline exceeded")
            # Resolve once per hop, check every returned address, then use
            # exactly those records while urllib3 opens the connection. The
            # robots callback is deliberately outside the pin so it can make
            # its own independently validated robots.txt request.
            normalized, records = _validated_destination(current_url, deadline=deadline)
            if allow_url is not None and not allow_url(normalized):
                raise AccessRestrictedError("Access is restricted by robots policy")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("Request deadline exceeded")
            try:
                parsed = urlsplit(normalized)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                with _pin_resolution(parsed.hostname or "", port, records, deadline=deadline):
                    response = self.session.get(
                        normalized,
                        headers=request_headers,
                        # Requests accepts urllib3's total timeout object.
                        # Unlike a scalar (which creates independent connect
                        # and read budgets), this leaves the header read only
                        # the portion of the one wall-clock deadline that
                        # connection setup did not consume.
                        timeout=Urllib3Timeout(total=remaining),
                        allow_redirects=False,
                        stream=True,
                    )
            except requests.RequestException as exc:
                raise FetchError("Network request failed") from exc
            try:
                _validate_actual_peer(response)
                if time.monotonic() >= deadline:
                    raise FetchError("Request deadline exceeded")
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchError("Redirect response had no location")
                    if redirect_count >= self.settings.max_redirects:
                        raise FetchError("Too many redirects")
                    current_url = urljoin(normalized, location)
                    continue
                body = read_bounded_response(response, self.settings.response_size_limit, deadline)
                return FetchResponse(
                    url=normalized,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=body,
                )
            except requests.RequestException as exc:
                raise FetchError("Response could not be read") from exc
            finally:
                response.close()
        raise FetchError("Too many redirects")


def _validate_actual_peer(response: requests.Response) -> None:
    """Check the peer address when urllib3 exposes it.

    The preflight DNS validation above is required even on adapters/mocks that
    do not expose a socket. On standard requests/urllib3 connections this
    second check catches a changed connection destination after resolution.
    """

    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        # Without an inspectable connected peer, a DNS preflight alone cannot
        # rule out rebinding between resolution and the actual connection.
        raise FetchError("Could not verify connection destination")
    try:
        peer = sock.getpeername()[0]
        _require_public_address(ipaddress.ip_address(peer))
    except OSError as exc:
        raise FetchError("Could not verify connection destination") from exc
    except ValueError as exc:
        raise UnsafeUrlError("Connection used an invalid destination") from exc


def read_bounded_response(response: requests.Response, maximum: int, deadline: float) -> bytes:
    """Read a response post-decompression within size and wall-clock limits."""

    chunks: list[bytes] = []
    total = 0
    raw = getattr(response, "raw", None)
    if raw is None or not hasattr(raw, "read"):
        raise FetchError("Response body could not be read safely")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("Request deadline exceeded")
        _set_read_timeout(response, remaining)
        try:
            try:
                # urllib3 decodes content here. This caps the actual bytes
                # which enter process memory, not just compressed wire bytes.
                chunk = raw.read(64 * 1024, decode_content=True)
            except TypeError:
                chunk = raw.read(64 * 1024)
        except (OSError, Urllib3HttpError) as exc:
            raise FetchError("Response body could not be read safely") from exc
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ResponseTooLargeError("Response exceeds the 2 MiB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_bounded_body(response: object, maximum: int) -> bytes:
    """Compatibility helper for small offline adapters and unit tests.

    Production requests use ``read_bounded_response`` with an explicit hard
    deadline. This adapter intentionally has no network path; it only supports
    already-provided ``iter_content`` fixtures.
    """

    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise FetchError("Response body could not be read safely")
    chunks: list[bytes] = []
    total = 0
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise ResponseTooLargeError("Response exceeds the 2 MiB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _set_read_timeout(response: requests.Response, seconds: float) -> None:
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
    sock = getattr(connection, "sock", None)
    if sock is not None:
        try:
            sock.settimeout(max(seconds, 0.001))
        except OSError as exc:
            raise FetchError("Could not enforce request deadline") from exc


class RobotsPolicy:
    """In-memory robots checks. No robots text is persisted or logged."""

    def __init__(self, client: SafeHttpClient, user_agent: str = "PYKASU-PoC") -> None:
        self.client = client
        self.user_agent = user_agent
        self._rules: dict[str, RobotFileParser | None] = {}

    def allows(self, article_url: str) -> bool:
        parsed = urlsplit(article_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._rules:
            robots_url = urljoin(origin + "/", "robots.txt")
            try:
                response = self.client.fetch(robots_url, {"Accept": "text/plain,*/*;q=0.1"})
            except FetchError:
                # Without a readable policy we conservatively do not fetch an
                # article body; feed metadata still remains usable.
                self._rules[origin] = None
            else:
                if response.status_code == 404:
                    parser = RobotFileParser()
                    parser.parse([])
                    self._rules[origin] = parser
                elif response.status_code == 200:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                    self._rules[origin] = parser
                else:
                    self._rules[origin] = None
        parser = self._rules[origin]
        return bool(parser and parser.can_fetch(self.user_agent, article_url))


class ArticleExtractor:
    """Fetch and extract an article transiently, respecting access boundaries."""

    def __init__(self, client: SafeHttpClient) -> None:
        self.client = client
        self.robots = RobotsPolicy(client)

    def extract(self, url: str) -> str:
        headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"}
        if isinstance(self.client, SafeHttpClient):
            response = self.client.fetch(
                url,
                headers,
                # SafeHttpClient invokes this before both the original request
                # and every redirect target, rather than checking robots only
                # once after content has been retrieved.
                allow_url=self.robots.allows,
            )
        else:
            # Injectable test/offline clients do not necessarily implement the
            # redirect hook. Keep the initial/final policy checks for that
            # narrow adapter boundary; production always takes the safe branch.
            if not self.robots.allows(url):
                raise AccessRestrictedError("Article access is restricted by robots policy")
            response = self.client.fetch(url, headers)
            if response.url != url and not self.robots.allows(response.url):
                raise AccessRestrictedError("Redirect target is restricted by robots policy")
        if response.status_code in {401, 403, 429, 451}:
            raise AccessRestrictedError("Article access is restricted by the publisher")
        if response.status_code != 200:
            raise FetchError(f"Article request returned HTTP {response.status_code}")
        html_text = response.body.decode(_response_encoding(response.headers), errors="replace")
        if has_tdm_reservation(response.headers, html_text):
            raise AccessRestrictedError("Article has a text-and-data-mining reservation")
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html_text,
                output_format="txt",
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception as exc:
            raise FetchError("Article extraction failed") from exc
        if not extracted or not extracted.strip():
            raise FetchError("No extractable article text was available")
        return extracted


def _response_encoding(headers: Mapping[str, str]) -> str:
    content_type = headers.get("Content-Type", "")
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def has_tdm_reservation(headers: Mapping[str, str], html_text: str) -> bool:
    """Recognize common machine-readable opt-outs before transformation."""

    for key, value in headers.items():
        if key.lower() in {"tdm-reservation", "x-tdm-reservation"} and str(value).strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return True
    combined_headers = " ".join(
        value
        for key, value in headers.items()
        if key.lower() in {"x-robots-tag", "tdm-reservation", "x-tdm-reservation"}
    ).lower()
    reservation_tokens = {"noai", "noimageai", "notrain", "no-tdm", "notdm", "tdm-reservation"}
    if any(token in combined_headers for token in reservation_tokens):
        return True
    if re.search(r"(?i)\b(?:tdm[-_ ]?reservation)\s*[:=]?\s*(?:1|true|yes)\b", combined_headers):
        return True
    # A compact parser-free check prevents an HTML body from reaching
    # Trafilatura when publishers communicate an opt-out in a robots meta tag.
    for tag in re.findall(r"<meta\b[^>]*>", html_text, flags=re.IGNORECASE):
        name = re.search(r"\bname\s*=\s*['\"]?([^'\"\s>]+)", tag, re.IGNORECASE)
        content = re.search(r"\bcontent\s*=\s*['\"]?([^'\">]+)", tag, re.IGNORECASE)
        if not name or not content:
            continue
        if name.group(1).lower() in {"robots", "tdm-reservation", "x-robots-tag"}:
            values = content.group(1).lower()
            if any(token in values for token in reservation_tokens) or values.strip() in {"1", "true", "yes"}:
                return True
    return False


def retry_after_timestamp(headers: Mapping[str, str]) -> str:
    """Return a provider retry deadline, defaulting safely to one hour."""

    raw = headers.get("Retry-After")
    now = utc_now()
    if raw:
        try:
            seconds = max(0, int(raw))
            return as_utc_text(now + timedelta(seconds=seconds))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is not None and parsed > now:
                    return as_utc_text(parsed)
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    return as_utc_text(now + timedelta(hours=1))
