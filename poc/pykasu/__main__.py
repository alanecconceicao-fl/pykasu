"""Run the local browser application with Waitress."""

from __future__ import annotations

from waitress import serve

from .app import create_app
from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    serve(app, host=settings.bind, port=settings.port)


if __name__ == "__main__":
    main()
