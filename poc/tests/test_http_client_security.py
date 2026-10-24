"""Offline regression tests for the bounded, pinned HTTP transport."""

from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from urllib3.util import Timeout as Urllib3Timeout

from pykasu.config import Settings
from pykasu import http_client
from pykasu.http_client import SafeHttpClient, _pin_resolution


class _PublicSocket:
    def getpeername(self) -> tuple[str, int]:
        return "8.8.8.8", 443

    def settimeout(self, _seconds: float) -> None:
        pass


class _RawBody:
    def __init__(self, body: bytes) -> None:
        self._connection = type("Connection", (), {"sock": _PublicSocket()})()
        self._remaining = body

    def read(self, _size: int, decode_content: bool = False) -> bytes:
        del decode_content
        value, self._remaining = self._remaining, b""
        return value


class _Response:
    status_code = 200
    headers: dict[str, str] = {}
    is_redirect = False
    is_permanent_redirect = False

    def __init__(self, body: bytes) -> None:
        self.raw = _RawBody(body)

    def close(self) -> None:
        pass


class _RecordingSession:
    trust_env = True

    def __init__(self) -> None:
        self.observed_records: list[tuple[object, ...]] | None = None

    def get(self, _url: str, **kwargs: object) -> _Response:
        timeout = kwargs["timeout"]
        if not isinstance(timeout, Urllib3Timeout):
            raise AssertionError("The request must use one total timeout budget")
        self.observed_records = socket.getaddrinfo(
            "source.example.test", 443, type=socket.SOCK_STREAM
        )
        return _Response(b"<rss />")


class HttpClientSecurityTests(unittest.TestCase):
    def test_safe_client_pins_the_validated_records_through_connection_setup(self) -> None:
        calls: list[tuple[str, int]] = []

        def resolver(
            host: str,
            port: int,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[object, ...]]:
            del family, type, proto, flags
            calls.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", resolver):
            session = _RecordingSession()
            client = SafeHttpClient(Settings(data_dir=Path(directory)), session=session)
            fetched = client.fetch("https://source.example.test/feed.xml")

        self.assertEqual(fetched.body, b"<rss />")
        self.assertEqual(calls, [("source.example.test", 443)])
        self.assertEqual(session.observed_records[0][4][0], "8.8.8.8")

    def test_concurrent_pins_restore_the_original_resolver(self) -> None:
        first_entered = threading.Event()
        second_waiting = threading.Event()
        release_first = threading.Event()
        errors: list[BaseException] = []

        class SignallingLock:
            """Expose the point after the second context captures resolver."""

            def __init__(self, lock: threading.RLock) -> None:
                self.lock = lock

            def acquire(self, *args: object, **kwargs: object) -> bool:
                if threading.current_thread().name == "second-pin":
                    second_waiting.set()
                return self.lock.acquire(*args, **kwargs)

            def release(self) -> None:
                self.lock.release()

        def resolver(
            host: str,
            port: int,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[object, ...]]:
            del host, family, type, proto, flags
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("9.9.9.9", port))]

        first_records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        second_records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]

        def first_pin() -> None:
            try:
                with _pin_resolution("first.example.test", 443, first_records):
                    first_entered.set()
                    if not release_first.wait(timeout=2):
                        raise AssertionError("second pin did not get a chance to wait")
            except BaseException as exc:
                errors.append(exc)

        def second_pin() -> None:
            try:
                if not first_entered.wait(timeout=2):
                    raise AssertionError("first pin did not start")
                with _pin_resolution("second.example.test", 443, second_records):
                    records = socket.getaddrinfo(
                        "second.example.test", 443, type=socket.SOCK_STREAM
                    )
                    if records != second_records:
                        raise AssertionError("second pin did not use its own records")
            except BaseException as exc:
                errors.append(exc)

        with patch("socket.getaddrinfo", resolver) as original:
            with patch.object(http_client, "_DNS_PATCH_LOCK", SignallingLock(http_client._DNS_PATCH_LOCK)):
                first = threading.Thread(target=first_pin, name="first-pin")
                second = threading.Thread(target=second_pin, name="second-pin")
                first.start()
                second.start()
                self.assertTrue(first_entered.wait(timeout=2))
                # At this point the second context has captured the currently
                # pinned resolver and is blocked on the first context's lock.
                self.assertTrue(second_waiting.wait(timeout=2))
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                self.assertIs(socket.getaddrinfo, original)


if __name__ == "__main__":
    unittest.main()
