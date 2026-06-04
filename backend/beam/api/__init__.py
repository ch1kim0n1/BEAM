"""FastAPI REST + WebSocket package (pdd.md section 12).

Implements the REST control plane (section 12.1), the WebSocket telemetry stream
(section 12.2), and client -> server control (section 12.3). All payloads are validated
against the contract models in :mod:`beam.schemas` (reused, never redefined) plus the
REST transport envelopes in :mod:`beam.api.models`.

Public entry points::

    from beam.api import create_app, serve
"""

from __future__ import annotations

from beam.api.app import create_app, serve

# Module-level ASGI app so uvicorn (and the ``beam serve`` CLI) can target it by the
# import string ``beam.api:app`` for reload/worker management.
app = create_app()

__all__ = ["create_app", "serve", "app"]
