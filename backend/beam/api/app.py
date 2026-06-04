"""FastAPI application factory + uvicorn entry point (pdd.md section 12).

:func:`create_app` builds the ASGI app: it installs a process-wide
:class:`~beam.api.runtime.Registry` on ``app.state``, mounts the REST router
(:mod:`beam.api.routes`, prefix ``/api``) and the WebSocket router
(:mod:`beam.api.ws`, ``/ws/run/{run_id}``), and enables permissive CORS so the
local frontend dev server can reach it. :func:`serve` runs it under uvicorn (wired into
the ``beam serve`` CLI subcommand).

Importing this module imports :mod:`beam.solvers`, which registers the full solver
suite, so ``GET /api/solvers`` is populated and runs can race every policy.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importing the solvers package registers every concrete solver in REGISTRY (side
# effect via @register). The catalog + race depend on this having happened.
import beam.solvers  # noqa: F401
from beam.api.routes import router as rest_router
from beam.api.runtime import Registry
from beam.api.ws import ws_router
from beam.schemas import SCHEMA_VERSION

__all__ = ["create_app", "serve"]


def create_app() -> FastAPI:
    """Build and return the BEAM FastAPI application."""
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
        allow_origins=["*"],
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
