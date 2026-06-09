"""Dashboard UI and HTTP server for by-framework observability."""

from .dashboard import make_handler, serve

__all__ = ["make_handler", "serve"]
