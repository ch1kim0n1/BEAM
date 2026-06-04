"""WebSocket telemetry + control endpoint (pdd.md sections 12.2, 12.3).

A client connects to ``/ws/run/{run_id}`` to:

- **receive** the server -> client telemetry stream: one :class:`FrameMessage` per
  simulation frame and one :class:`EpochMessage` per decision epoch (pdd.md 12.2),
  serialized as JSON with ``BeamFrame`` keyed ``"from"`` (alias) and the contract's
  ``schema_version`` on every message;
- **send** client -> server :class:`~beam.schemas.ControlMessage` commands
  (pause/resume/step/stop/set_solver/set_speed, pdd.md 12.3), validated server-side.

Connecting starts the run's driver (if not already running) and subscribes the socket
to the controller's fan-out queue. Two concurrent tasks run for the connection lifetime:
a *pump* that drains the subscriber queue to the socket, and a *reader* that parses and
applies inbound control messages. The connection closes when the run ends (a sentinel
``{"type":"end",...}`` is forwarded) or the client disconnects.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from beam.api.runtime import Registry, RunController
from beam.schemas import ControlMessage

ws_router = APIRouter()

__all__ = ["ws_router", "apply_control"]


def apply_control(controller: RunController, msg: ControlMessage) -> dict[str, Any]:
    """Apply a validated control message to a controller; return an ack dict.

    Shared by the WS reader and (indirectly) the REST control route's semantics so the
    behavior is identical on both transports (pdd.md 12.3). Raises ``ValueError`` for
    semantically invalid actions (unknown solver, bad multiplier, missing field).
    """
    action = msg.action
    if action == "pause":
        controller.pause()
    elif action == "resume":
        controller.resume()
    elif action == "step":
        controller.step(msg.epochs if msg.epochs is not None else 1)
    elif action == "stop":
        controller.stop()
    elif action == "set_solver":
        if msg.solver is None:
            raise ValueError("set_solver requires 'solver'")
        controller.set_solver(msg.solver)
    elif action == "set_speed":
        if msg.multiplier is None:
            raise ValueError("set_speed requires 'multiplier'")
        controller.set_speed(msg.multiplier)
    return {
        "type": "ack",
        "action": action,
        "status": controller.status,
        "active_solver": controller.active_solver,
        "speed": controller.speed,
    }


@ws_router.websocket("/ws/run/{run_id}")
async def run_telemetry(websocket: WebSocket, run_id: str) -> None:
    reg: Registry | None = getattr(websocket.app.state, "registry", None)
    controller = reg.runs.get(run_id) if reg is not None else None

    await websocket.accept()
    if controller is None:
        await websocket.send_json({"type": "error", "detail": f"unknown run {run_id!r}"})
        await websocket.close()
        return

    queue = controller.subscribe()
    # Start (or attach to) the deterministic driver. Idempotent: many sockets, one run.
    controller.start()

    async def pump() -> None:
        """Drain the subscriber queue to the socket until the run ends."""
        while True:
            message = await queue.get()
            await websocket.send_json(message)
            if message.get("type") == "end":
                break

    async def reader() -> None:
        """Parse + apply inbound control messages until the client disconnects."""
        while True:
            raw = await websocket.receive_json()
            try:
                ctrl = ControlMessage.model_validate(raw)
            except ValidationError as exc:
                await websocket.send_json({"type": "error", "detail": exc.errors()})
                continue
            try:
                ack = apply_control(controller, ctrl)
            except ValueError as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                continue
            await websocket.send_json(ack)

    pump_task = asyncio.ensure_future(pump())
    reader_task = asyncio.ensure_future(reader())
    try:
        done, pending = await asyncio.wait(
            {pump_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pump_task.cancel()
        reader_task.cancel()
    finally:
        controller.unsubscribe(queue)
        try:
            await websocket.close()
        except RuntimeError:  # already closed
            pass
