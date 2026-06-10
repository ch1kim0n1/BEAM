"""FastAPI application factory + uvicorn entry point (pdd.md section 12).

:func:`create_app` builds the ASGI app: it installs a process-wide
:class:`~beam.api.runtime.Registry` on ``app.state``, mounts the REST router
(:mod:`beam.api.routes`, prefix ``/api``) and the WebSocket router
(:mod:`beam.api.ws`, ``/ws/run/{run_id}``), and configures CORS. :func:`serve`
runs it under uvicorn (wired into the ``beam serve`` CLI subcommand).

Importing this module imports :mod:`beam.solvers`, which registers the full solver
suite, so ``GET /api/solvers`` is populated and runs can race every policy.
"""

from __future__ import annotations

import logging
import logging.config
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# CORS: defaults to the Vite dev server origins (the only legitimate callers during
# local development). Restrict to the actual deployment origin(s) in production by
# setting CORS_ORIGINS as a comma-separated list, e.g.:
#   CORS_ORIGINS=https://beam.example.com
# Never leave as "*" in production.
_DEV_ORIGINS = "http://localhost:5173,http://localhost:5174"
CORS_ORIGINS: list[str] = os.getenv("CORS_ORIGINS", _DEV_ORIGINS).split(",")


def configure_logging() -> None:
    """Configure structured logging for the BEAM server process.

    Uses Python's standard logging at INFO by default; set BEAM_LOG_LEVEL (e.g.
    DEBUG, WARNING) to override. All log output goes to stdout so container
    runtimes and process supervisors can capture it.
    """
    level = os.getenv("BEAM_LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                }
            },
            "root": {"level": level, "handlers": ["stdout"]},
            # Quieten uvicorn's duplicate access log - its own handler handles it.
            "loggers": {
                "uvicorn.access": {"propagate": False},
            },
        }
    )


# Importing the solvers package registers every concrete solver in REGISTRY (side
# effect via @register). The catalog + race depend on this having happened.
import beam.solvers  # noqa: F401
from beam.api.routes import router as rest_router
from beam.api.runtime import Registry
from beam.api.ws import ws_router
from beam.schemas import SCHEMA_VERSION

__all__ = ["create_app", "serve"]

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build and return the BEAM FastAPI application."""
    configure_logging()
    log.info(
        "BEAM API starting  schema_version=%s  cors_origins=%s",
        SCHEMA_VERSION,
        CORS_ORIGINS,
    )

    app = FastAPI(
        title="BEAM API",
        version=SCHEMA_VERSION,
        description=(
            "Laser battery vs drone-swarm DWTA simulation / OR-analysis tool. "
            "REST control plane + WebSocket telemetry (pdd.md section 12)."
        ),
    )
    app.state.registry = Registry()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(rest_router)
    app.include_router(ws_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "schema_version": SCHEMA_VERSION}

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the app under uvicorn (the ``beam serve`` CLI body)."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
