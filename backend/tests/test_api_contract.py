"""API contract tests (pdd.md sections 12.1, 12.2, 12.3).

Covers the REST surface and the WebSocket telemetry/control plane:

- every REST endpoint validates its input and its output against the contract schemas
  (the :mod:`beam.api.models` envelopes reuse, never redefine, :mod:`beam.schemas`);
- a short run streams well-formed ``frame`` + ``epoch`` telemetry over a test
  WebSocket, with the right ``schema_version`` and the ``BeamFrame`` ``"from"`` alias;
- client -> server control messages are validated by :class:`ControlMessage` and applied
  (pause/resume/step/stop/set_solver/set_speed).

Runs are kept tiny + fast by using a small-swarm scenario overlay and a fast solver
suite (greedy heuristics only — no OR-Tools), so the suite is quick and deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from beam.api import create_app
from beam.schemas import SCHEMA_VERSION
from beam.api.models import (
    BatchResultsResponse as BatchResults,
    RunStartResponse,
    RunSummaryResponse,
    ScenarioCreateResponse,
    ScenarioGetResponse,
    SolversListResponse,
    WeatherListResponse,
)

# A deliberately tiny, fast-resolving scenario: few drones, a close spawn radius so the
# swarm reaches the asset (and the run terminates) in a handful of epochs, and a fast
# heuristic-only solver suite (no cp_sat / metaheuristic) for snappy tests.
FAST_SCENARIO_OVERLAY: dict[str, Any] = {
    "id": "api_test",
    "swarm_spec": {
        "count": 4,
        "behavior": "direct",
        "speed": 400.0,
        "spawn_radius": 600.0,
        "spawn_arc_deg": [0.0, 360.0],
        "class_mix": {"quad_small": 1.0},
    },
    "seed": 7,
}
FAST_SOLVERS = ["greedy_nearest", "greedy_threat", "greedy_urgent"]


@pytest.fixture()
def client() -> TestClient:
    """A TestClient over a fresh app (isolated registry per test)."""
    app = create_app()
    with TestClient(app) as c:
        yield c


def _create_scenario(client: TestClient, overlay: dict[str, Any] | None = None) -> str:
    body = {"preset": "swarm_24", "scenario": overlay or FAST_SCENARIO_OVERLAY}
    resp = client.post("/api/scenario", json=body)
    assert resp.status_code == 200, resp.text
    parsed = ScenarioCreateResponse.model_validate(resp.json())  # output schema check
    return parsed.scenario_id


# --------------------------------------------------------------------------- #
# Catalog endpoints                                                           #
# --------------------------------------------------------------------------- #


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["schema_version"] == SCHEMA_VERSION


def test_list_solvers(client: TestClient) -> None:
    resp = client.get("/api/solvers")
    assert resp.status_code == 200, resp.text
    parsed = SolversListResponse.model_validate(resp.json())
    names = [s.name for s in parsed.solvers]
    # Contract build-order solvers must all be present.
    for expected in ["greedy_nearest", "greedy_threat", "greedy_urgent", "auction", "cp_sat"]:
        assert expected in names
    assert parsed.reference == "cp_sat"
    assert any(s.is_reference for s in parsed.solvers)
    assert parsed.schema_version == SCHEMA_VERSION


def test_list_weather(client: TestClient) -> None:
    resp = client.get("/api/weather")
    assert resp.status_code == 200, resp.text
    parsed = WeatherListResponse.model_validate(resp.json())
    names = {p.name for p in parsed.profiles}
    assert {"clear", "haze", "rain", "fog", "dust"}.issubset(names)
    # alpha ordering sanity (config-driven, not hard-coded here).
    by_name = {p.name: p.alpha for p in parsed.profiles}
    assert by_name["clear"] < by_name["dust"]


# --------------------------------------------------------------------------- #
# Scenario endpoints                                                          #
# --------------------------------------------------------------------------- #


def test_create_and_get_scenario(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    resp = client.get(f"/api/scenario/{scenario_id}")
    assert resp.status_code == 200, resp.text
    parsed = ScenarioGetResponse.model_validate(resp.json())
    assert parsed.scenario_id == scenario_id
    # Overlay merged on top of the preset.
    assert parsed.scenario["swarm_spec"]["count"] == 4
    assert parsed.scenario["id"] == "api_test"


def test_create_scenario_preset_only(client: TestClient) -> None:
    resp = client.post("/api/scenario", json={"preset": "swarm_24"})
    assert resp.status_code == 200, resp.text
    parsed = ScenarioCreateResponse.model_validate(resp.json())
    got = client.get(f"/api/scenario/{parsed.scenario_id}").json()
    assert got["scenario"]["swarm_spec"]["count"] == 24


def test_create_scenario_requires_input(client: TestClient) -> None:
    resp = client.post("/api/scenario", json={})
    assert resp.status_code == 422


def test_create_scenario_rejects_extra_field(client: TestClient) -> None:
    resp = client.post("/api/scenario", json={"preset": "swarm_24", "bogus": 1})
    assert resp.status_code == 422  # extra="forbid" on the request envelope


def test_create_scenario_unknown_preset(client: TestClient) -> None:
    resp = client.post("/api/scenario", json={"preset": "does_not_exist"})
    assert resp.status_code == 404


def test_get_unknown_scenario(client: TestClient) -> None:
    resp = client.get("/api/scenario/scn_999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Run lifecycle endpoints                                                     #
# --------------------------------------------------------------------------- #


def test_start_run(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    resp = client.post(
        "/api/run",
        json={"scenario_id": scenario_id, "solver": "greedy_nearest", "enabled_solvers": FAST_SOLVERS},
    )
    assert resp.status_code == 200, resp.text
    parsed = RunStartResponse.model_validate(resp.json())
    assert parsed.active_solver == "greedy_nearest"
    assert set(FAST_SOLVERS).issubset(set(parsed.enabled_solvers))


def test_start_run_defaults_active_solver(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    resp = client.post("/api/run", json={"scenario_id": scenario_id})
    assert resp.status_code == 200, resp.text
    parsed = RunStartResponse.model_validate(resp.json())
    assert parsed.active_solver == "cp_sat"  # reference is the default active solver


def test_start_run_unknown_scenario(client: TestClient) -> None:
    resp = client.post("/api/run", json={"scenario_id": "scn_999"})
    assert resp.status_code == 404


def test_start_run_unknown_solver(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    resp = client.post("/api/run", json={"scenario_id": scenario_id, "solver": "nope"})
    assert resp.status_code == 422


def test_run_control_and_summary(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    run_id = RunStartResponse.model_validate(
        client.post(
            "/api/run",
            json={
                "scenario_id": scenario_id,
                "solver": "greedy_nearest",
                "enabled_solvers": FAST_SOLVERS,
            },
        ).json()
    ).run_id

    # set_speed (valid).
    resp = client.post(f"/api/run/{run_id}/control", json={"action": "set_speed", "multiplier": 4.0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["speed"] == 4.0

    # set_solver to an enabled policy.
    resp = client.post(f"/api/run/{run_id}/control", json={"action": "set_solver", "solver": "greedy_threat"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["active_solver"] == "greedy_threat"

    # pause/resume acknowledged.
    assert client.post(f"/api/run/{run_id}/control", json={"action": "pause"}).status_code == 200

    # stop, then summary should be retrievable and well-formed.
    assert client.post(f"/api/run/{run_id}/control", json={"action": "stop"}).status_code == 200

    resp = client.get(f"/api/run/{run_id}/summary")
    assert resp.status_code == 200, resp.text
    parsed = RunSummaryResponse.model_validate(resp.json())
    assert parsed.run_id == run_id
    assert "telemetry_ws" in parsed.artifacts


def test_run_control_invalid_payloads(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    run_id = RunStartResponse.model_validate(
        client.post(
            "/api/run",
            json={"scenario_id": scenario_id, "enabled_solvers": FAST_SOLVERS, "solver": "greedy_nearest"},
        ).json()
    ).run_id

    # Unknown action -> ControlMessage validation rejects (Literal).
    assert client.post(f"/api/run/{run_id}/control", json={"action": "warp"}).status_code == 422
    # set_solver missing 'solver'.
    assert client.post(f"/api/run/{run_id}/control", json={"action": "set_solver"}).status_code == 422
    # set_solver to a non-enabled solver.
    assert (
        client.post(
            f"/api/run/{run_id}/control", json={"action": "set_solver", "solver": "auction"}
        ).status_code
        == 422
    )
    # set_speed with a non-positive multiplier.
    assert (
        client.post(
            f"/api/run/{run_id}/control", json={"action": "set_speed", "multiplier": 0.0}
        ).status_code
        == 422
    )
    # Extra field on the control message -> 422 (extra="forbid").
    assert (
        client.post(
            f"/api/run/{run_id}/control", json={"action": "pause", "junk": True}
        ).status_code
        == 422
    )


def test_control_unknown_run(client: TestClient) -> None:
    resp = client.post("/api/run/run_999/control", json={"action": "pause"})
    assert resp.status_code == 404


def test_summary_unknown_run(client: TestClient) -> None:
    resp = client.get("/api/run/run_999/summary")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# WebSocket telemetry + control (pdd.md 12.2 / 12.3)                           #
# --------------------------------------------------------------------------- #


def _validate_frame(msg: dict[str, Any]) -> None:
    assert msg["type"] == "frame"
    assert msg["schema_version"] == SCHEMA_VERSION
    assert isinstance(msg["t"], (int, float))
    for d in msg["drones"]:
        assert {"id", "x", "y", "v", "value", "hp_frac", "state", "tti"} <= set(d)
        assert len(d["v"]) == 2
        assert 0.0 <= d["hp_frac"] <= 1.0
    for tt in msg["turrets"]:
        assert {"id", "x", "y", "aim", "state", "thermal_frac"} <= set(tt)
        assert 0.0 <= tt["thermal_frac"] <= 1.0
    for b in msg["beams"]:
        # BeamFrame must serialize with the "from" alias, not "from_".
        assert "from" in b and "from_" not in b
        assert {"from", "to", "power_frac"} <= set(b)


def _validate_epoch(msg: dict[str, Any]) -> None:
    assert msg["type"] == "epoch"
    assert msg["schema_version"] == SCHEMA_VERSION
    assert isinstance(msg["epoch"], int)
    assert {"cumulative_cost", "value_destroyed", "net"} <= set(msg["ledger"])
    assert isinstance(msg["solvers"], list)
    for s in msg["solvers"]:
        assert {"name", "objective", "solve_ms"} <= set(s)


def test_ws_streams_telemetry(client: TestClient) -> None:
    scenario_id = _create_scenario(client)
    run_id = RunStartResponse.model_validate(
        client.post(
            "/api/run",
            json={
                "scenario_id": scenario_id,
                "solver": "greedy_nearest",
                "enabled_solvers": FAST_SOLVERS,
            },
        ).json()
    ).run_id

    saw_frame = False
    saw_epoch = False
    saw_end = False
    with client.websocket_connect(f"/ws/run/{run_id}") as ws:
        # Speed the run up so it terminates quickly over the socket.
        ws.send_json({"action": "set_speed", "multiplier": 1000.0})

        for _ in range(2000):
            msg = ws.receive_json()
            mtype = msg.get("type")
            if mtype == "frame":
                _validate_frame(msg)
                saw_frame = True
            elif mtype == "epoch":
                _validate_epoch(msg)
                saw_epoch = True
            elif mtype == "ack":
                assert msg["action"] == "set_speed"
            elif mtype == "end":
                saw_end = True
                assert msg["status"] in ("finished", "stopped")
                break

    assert saw_frame, "expected at least one frame message"
    assert saw_epoch, "expected at least one epoch message"
    assert saw_end, "expected an end sentinel when the run completed"

    # Summary is populated after the run ends.
    summary = RunSummaryResponse.model_validate(client.get(f"/api/run/{run_id}/summary").json())
    assert summary.status in ("finished", "stopped")
    assert summary.summary is not None
    assert summary.telemetry_hash


def test_ws_control_stop(client: TestClient) -> None:
    """A stop control over the socket terminates the run with status 'stopped'."""
    # Slow swarm so the run won't naturally finish before we stop it.
    slow_overlay = dict(FAST_SCENARIO_OVERLAY)
    slow_overlay["swarm_spec"] = dict(FAST_SCENARIO_OVERLAY["swarm_spec"])
    slow_overlay["swarm_spec"]["speed"] = 1.0
    slow_overlay["swarm_spec"]["spawn_radius"] = 5000.0
    scenario_id = _create_scenario(client, slow_overlay)

    run_id = RunStartResponse.model_validate(
        client.post(
            "/api/run",
            json={
                "scenario_id": scenario_id,
                "solver": "greedy_nearest",
                "enabled_solvers": FAST_SOLVERS,
            },
        ).json()
    ).run_id

    with client.websocket_connect(f"/ws/run/{run_id}") as ws:
        ws.send_json({"action": "set_speed", "multiplier": 1000.0})
        # Receive a few messages, then stop.
        for _ in range(5):
            ws.receive_json()
        ws.send_json({"action": "stop"})
        # Drain until the end sentinel.
        saw_end = False
        for _ in range(2000):
            msg = ws.receive_json()
            if msg.get("type") == "end":
                assert msg["status"] == "stopped"
                saw_end = True
                break
    assert saw_end


def test_ws_unknown_run(client: TestClient) -> None:
    with client.websocket_connect("/ws/run/run_999") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"


# --------------------------------------------------------------------------- #
# Batch endpoints (pdd.md 12.1, 10)                                           #
# --------------------------------------------------------------------------- #


def test_batch_sweep(client: TestClient) -> None:
    # Tiny inline sweep spec: two swarm sizes, two fast solvers.
    sweep_spec = {
        "id": "api_test_sweep",
        "base_scenario": "swarm_24",
        "seed": 7,
        "sweep": {"parameter": "swarm_spec.count", "values": [2, 4]},
        "solvers": ["greedy_nearest", "greedy_threat"],
        "outputs": ["net_position_vs_swarm_size", "gap_vs_swarm_size"],
    }
    resp = client.post("/api/batch", json={"sweep_spec": sweep_spec})
    assert resp.status_code == 200, resp.text
    batch_id = resp.json()["batch_id"]

    # Poll results until the background job finishes.
    parsed: BatchResults | None = None
    for _ in range(200):
        r = client.get(f"/api/batch/{batch_id}/results")
        assert r.status_code == 200, r.text
        parsed = BatchResults.model_validate(r.json())
        if parsed.status in ("finished", "error"):
            break
    assert parsed is not None
    assert parsed.status == "finished", parsed.model_dump()
    assert parsed.parameter == "swarm_spec.count"
    assert parsed.values == [2.0, 4.0]
    assert "net_position_vs_swarm_size" in parsed.series
    assert set(parsed.series["net_position_vs_swarm_size"].keys()) == {
        "greedy_nearest",
        "greedy_threat",
    }
    # Each net series has one point per swept value.
    for net in parsed.series["net_position_vs_swarm_size"].values():
        assert len(net) == 2


def test_batch_unknown_sweep(client: TestClient) -> None:
    resp = client.post("/api/batch", json={"sweep": "no_such_sweep"})
    assert resp.status_code == 404


def test_batch_requires_input(client: TestClient) -> None:
    resp = client.post("/api/batch", json={})
    assert resp.status_code == 422


def test_batch_unknown_results(client: TestClient) -> None:
    resp = client.get("/api/batch/batch_999/results")
    assert resp.status_code == 404
