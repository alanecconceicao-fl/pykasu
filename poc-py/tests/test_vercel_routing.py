"""The Flask app remains routeable below the shared Vercel service prefix."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pykasu.app import create_app
from pykasu.config import Settings


class VercelRoutingTests(unittest.TestCase):
    def test_prefixed_routes_and_asset_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(data_dir=Path(temporary_directory))
            with patch.dict(os.environ, {"PYKASU_BASE_PATH": "/poc-py"}):
                app = create_app(settings)

            client = app.test_client()
            response = client.get("/poc-py/sources")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'data-base-path="/poc-py"', response.data)
            self.assertIn(b'/poc-py/static/app.css', response.data)
            self.assertIn(b'action="/poc-py/sources"', response.data)

            asset = client.get("/poc-py/static/app.css")
            self.assertEqual(asset.status_code, 200)
            self.assertIn(b"--ink:", asset.data)

            manager = app.extensions["pykasu_refresh_manager"]
            worker = manager._active_thread
            if worker is not None:
                worker.join(timeout=2)
