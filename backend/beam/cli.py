"""BEAM command-line entrypoint.

Subcommands:
  - serve : launch the FastAPI + WebSocket server (uvicorn against ``beam.api:app``)
  - run   : run a single headless scenario and emit telemetry (JSONL + summary)
  - batch : run a headless sweep and emit the breakeven + gap-vs-scale series

Console script registered in pyproject.toml as ``beam = "beam.cli:main"``.

Determinism (pdd.md 7.4): ``run`` and ``batch`` drive the deterministic headless loop;
the seed flows from the scenario/sweep spec (overridable on ``run`` via ``--seed``), and
no physics/cost constant is introduced here - everything flows from :mod:`beam.config`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beam",
        description="BEAM - laser battery vs drone swarm DWTA simulation / OR tool.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="launch the REST + WebSocket server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=_cmd_serve)

    # run
    p_run = sub.add_parser("run", help="run a single headless scenario")
    p_run.add_argument("--scenario", required=True, help="scenario preset name")
    p_run.add_argument("--solver", default=None, help="active solver name")
    p_run.add_argument("--seed", type=int, default=None, help="override scenario seed")
    p_run.add_argument("--out", default=None, help="telemetry output path")
    p_run.set_defaults(func=_cmd_run)

    # evolve
    p_evolve = sub.add_parser("evolve", help="evolve swarm attack params against a defender")
    p_evolve.add_argument("--scenario", default="swarm_24", help="base preset scenario name")
    p_evolve.add_argument("--solver", default="greedy_urgent", help="defender solver to evolve against")
    p_evolve.add_argument("--generations", type=int, default=30, help="number of GA generations")
    p_evolve.add_argument("--population", type=int, default=40, help="GA population size")
    p_evolve.add_argument("--seed", type=int, default=42, help="RNG seed")
    p_evolve.add_argument("--count", type=int, default=24, help="swarm drone count")
    p_evolve.add_argument("--behavior", default="flocking", help="swarm behavior (direct/flocking/staggered)")
    p_evolve.add_argument("--out", default=None, help="output YAML path (default: runs/evolved_<scenario>.yaml)")
    p_evolve.set_defaults(func=_cmd_evolve)  # type: ignore[name-defined]

    # batch
    p_batch = sub.add_parser("batch", help="run a headless sweep")
    p_batch.add_argument("--sweep", required=True, help="sweep spec name or .yaml path")
    p_batch.add_argument(
        "--scenario",
        default=None,
        help="override the sweep's base_scenario (preset name or .yaml path)",
    )
    p_batch.add_argument(
        "--out",
        default=None,
        help="output base dir for artifacts (default: ./, writing under runs/<id>/)",
    )
    p_batch.set_defaults(func=_cmd_batch)

    return parser


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _resolve_scenario_name(value: str) -> str:
    """Accept either a preset name (``swarm_24``) or a path (``config/scenarios/x.yaml``).

    ``beam.config.load_config`` takes a *preset name*; the README quickstart passes a
    path. We normalize a path down to its stem so both forms work identically.
    """
    if value.endswith((".yaml", ".yml")) or "/" in value or "\\" in value:
        return Path(value).stem
    return value


def _cmd_serve(args: argparse.Namespace) -> int:
    """Launch the FastAPI + WebSocket server via uvicorn (pdd.md 11, 12)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        print(f"beam serve: uvicorn is not installed ({exc})", file=sys.stderr)
        return 1

    # The API agent owns ``beam.api``; we target its ASGI ``app`` by import string so
    # uvicorn can manage reload/workers itself. Fail clearly if it is not wired yet.
    app_target = "beam.api:app"
    try:
        import beam.api as _api

        if not hasattr(_api, "app"):
            print(
                "beam serve: beam.api defines no ASGI 'app' yet "
                "(the API server is not wired).",
                file=sys.stderr,
            )
            return 1
    except ImportError as exc:  # pragma: no cover
        print(f"beam serve: cannot import beam.api ({exc})", file=sys.stderr)
        return 1

    uvicorn.run(app_target, host=args.host, port=args.port)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a single headless scenario to a telemetry log + summary (pdd.md 7.4, 12.2)."""
    from beam.config import load_config
    from beam.engine.loop import run_headless
    from beam.solvers import available_solvers

    scenario = _resolve_scenario_name(args.scenario)
    cfg = load_config(scenario=scenario)
    if cfg.scenario is None:
        print(f"beam run: scenario '{scenario}' not found", file=sys.stderr)
        return 1

    if args.seed is not None:
        cfg.scenario["seed"] = int(args.seed)

    # Active solver: explicit, else the configured metaheuristic, else the first greedy.
    active = args.solver
    if active is None:
        active = cfg.solver.metaheuristic if cfg.solver.metaheuristic in available_solvers() else "greedy_urgent"
    if active not in available_solvers():
        print(
            f"beam run: unknown solver '{active}'. "
            f"available: {', '.join(available_solvers())}",
            file=sys.stderr,
        )
        return 1

    out_dir = args.out if args.out is not None else "."
    result = run_headless(cfg, active_solver=active, out_dir=out_dir)

    summary = result.summary
    print(
        json.dumps(
            {
                "run_id": summary.run_id,
                "active_solver": active,
                "kills": summary.kills,
                "leaks": summary.leaks,
                "leaked_value": summary.leaked_value,
                "net_position": summary.final_ledger.net,
                "cumulative_cost": summary.final_ledger.cumulative_cost,
                "value_destroyed": summary.final_ledger.value_destroyed,
                "telemetry_hash": result.telemetry_hash,
                "out_dir": result.out_dir,
            },
            indent=2,
        )
    )
    return 0


def _cmd_evolve(args: argparse.Namespace) -> int:
    """Evolve swarm approach parameters to maximize leaks against a defender (plan spec)."""
    import yaml  # pyyaml; listed in pyproject.toml deps
    from beam.evolve.ga import GAConfig, run_ga
    from beam.evolve.genome import genome_to_overlay

    scenario = _resolve_scenario_name(args.scenario)
    cfg = GAConfig(
        base_scenario=scenario,
        defender_solver=args.solver,
        seed=args.seed,
        population_size=args.population,
        generations=args.generations,
        base_swarm_count=args.count,
        base_behavior=args.behavior,
    )

    def _progress(gen: int, best: float, mean: float) -> None:
        print(f"  gen {gen:3d}/{args.generations}  best={best:.0f}  mean={mean:.0f}")

    print(f"Evolving swarm against {args.solver!r} for {args.generations} generations...")
    result = run_ga(cfg, on_generation=_progress)

    if result.best_genome is None:
        print("beam evolve: no genome produced (empty population?)", file=sys.stderr)
        return 1

    overlay = genome_to_overlay(
        result.best_genome,
        base_swarm_count=args.count,
        base_behavior=args.behavior,
    )
    overlay["_source"] = "beam evolve"
    overlay["_fitness"] = result.best_fitness

    out_path = args.out or f"runs/evolved_{scenario}.yaml"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(yaml.dump(overlay, sort_keys=True))
    print(f"Best genome saved to {out_path}  (leaked_value={result.best_fitness:.0f})")
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    """Run a headless sweep -> breakeven crossover + gap-vs-scale CSVs (pdd.md 10, 15)."""
    from beam.batch.sweeps import load_sweep_spec, run_sweep
    from dataclasses import replace

    # Accept a sweep preset name or an explicit .yaml path.
    if args.sweep.endswith((".yaml", ".yml")) or "/" in args.sweep or "\\" in args.sweep:
        spec = load_sweep_spec(Path(args.sweep).stem, sweep_path=Path(args.sweep))
    else:
        spec = load_sweep_spec(args.sweep)

    if args.scenario is not None:
        spec = replace(spec, base_scenario=_resolve_scenario_name(args.scenario))

    out_dir = args.out if args.out is not None else "."
    result = run_sweep(spec, out_dir=out_dir)

    be = result.breakeven
    print(
        json.dumps(
            {
                "sweep_id": result.id,
                "parameter": result.parameter,
                "active_solver": result.active_solver,
                "points": len(result.points),
                "breakeven_crossover": {
                    "crossed": be.crossed,
                    "value": be.value,
                    "from_value": be.from_value,
                    "to_value": be.to_value,
                    "direction": be.direction,
                },
                "out_dir": result.out_dir,
            },
            indent=2,
        )
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
