"""Vercel Python entrypoint for the Flask PoC service."""

import os

os.environ.setdefault("PYKASU_BASE_PATH", "/poc-py")

from pykasu.app import create_app

app = create_app()
