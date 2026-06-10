"""Tournament GA over swarm approach genomes."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from beam.batch.sweeps import SweepSpec, _build_point_config
from beam.config import BeamConfig
from beam.engine.loop import LoopConfig, run_headless
from beam.evolve.genome import Genome, crossover, genome_to_overlay, mutate, random_genome

log = logging.getLogger(__name__)

__all__ = ["GAConfig", "GAResult", "run_ga"]


@dataclass(frozen=True)
class GAConfig:
    base_scenario: str
    defender_solver: str = "greedy_urgent"
    seed: int = 42
    population_size: int = 40
    generations: int = 30
    mutation_rate: float = 0.15
    crossover_rate: float = 0.70
    elite_count: int = 4
    base_swarm_count: int = 24
    base_behavior: str = "flocking"


@dataclass
class GAResult:
    best_genome: Optional[Genome]
    best_fitness: float
    generation_best: list[float] = field(default_factory=list)


def _evaluate(
    genome: Genome,
    base_cfg: BeamConfig,
    defender_solver: str,
    base_swarm_count: int,
    base_behavior: str,
    loop_cfg: Optional[LoopConfig] = None,
) -> float:
    cfg = copy.deepcopy(base_cfg)
    overlay = genome_to_overlay(
        genome,
        base_swarm_count=base_swarm_count,
        base_behavior=base_behavior,
    )
    if cfg.scenario is None:
        cfg.scenario = {}
    from beam.api.runtime import merge_overlay
    cfg.scenario = merge_overlay(cfg.scenario, overlay)
    result = run_headless(
        cfg,
        active_solver=defender_solver,
        enabled_solvers=[defender_solver],
        loop_cfg=loop_cfg,
    )
    return float(result.summary.leaked_value)


def _tournament(
    population: list[Genome],
    fitnesses: list[float],
    rng: np.random.Generator,
    k: int = 3,
) -> Genome:
    indices = rng.choice(len(population), size=min(k, len(population)), replace=False)
    best_idx = int(max(indices, key=lambda i: fitnesses[i]))
    return population[best_idx]


def run_ga(
    cfg: GAConfig,
    *,
    loop_cfg: Optional[LoopConfig] = None,
    on_generation: Optional[Callable[[int, float, float], None]] = None,
) -> GAResult:
    """Run tournament GA and return the best genome found."""
    rng = np.random.default_rng(cfg.seed)

    sweep_spec = SweepSpec(
        id="evolve",
        base_scenario=cfg.base_scenario,
        seed=cfg.seed,
        parameter="swarm_spec.count",
        values=[cfg.base_swarm_count],
        solvers=[cfg.defender_solver],
    )
    base_cfg = _build_point_config(sweep_spec, value=cfg.base_swarm_count)

    population = [random_genome(rng) for _ in range(cfg.population_size)]
    fitnesses = [
        _evaluate(g, base_cfg, cfg.defender_solver, cfg.base_swarm_count, cfg.base_behavior, loop_cfg)
        for g in population
    ]

    best_genome = population[int(np.argmax(fitnesses))]
    best_fitness = float(max(fitnesses))
    generation_best: list[float] = []

    for gen in range(cfg.generations):
        sorted_idx = sorted(range(len(population)), key=lambda i: fitnesses[i], reverse=True)
        elites = [population[i] for i in sorted_idx[: cfg.elite_count]]

        offspring: list[Genome] = []
        while len(offspring) < cfg.population_size - cfg.elite_count:
            if rng.random() < cfg.crossover_rate:
                a = _tournament(population, fitnesses, rng)
                b = _tournament(population, fitnesses, rng)
                c1, c2 = crossover(a, b, rng)
                offspring.extend([mutate(c1, rng, rate=cfg.mutation_rate),
                                   mutate(c2, rng, rate=cfg.mutation_rate)])
            else:
                p = _tournament(population, fitnesses, rng)
                offspring.append(mutate(p, rng, rate=cfg.mutation_rate))

        population = elites + offspring[: cfg.population_size - cfg.elite_count]
        fitnesses = [
            _evaluate(g, base_cfg, cfg.defender_solver, cfg.base_swarm_count, cfg.base_behavior, loop_cfg)
            for g in population
        ]

        gen_best = float(max(fitnesses))
        gen_mean = float(np.mean(fitnesses))
        generation_best.append(gen_best)

        if gen_best > best_fitness:
            best_fitness = gen_best
            best_genome = population[int(np.argmax(fitnesses))]

        log.info("gen %d/%d  best=%.1f  mean=%.1f", gen + 1, cfg.generations, gen_best, gen_mean)
        if on_generation:
            on_generation(gen + 1, gen_best, gen_mean)

    return GAResult(
        best_genome=best_genome,
        best_fitness=best_fitness,
        generation_best=generation_best,
    )
