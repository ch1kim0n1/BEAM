import pytest
import numpy as np
from beam.evolve.genome import Genome, BOUNDS, genome_to_overlay, random_genome, crossover, mutate


def test_genome_to_overlay_contains_swarm_spec():
    g = Genome(
        spawn_arc_center_deg=90.0, spawn_arc_width_deg=120.0, spawn_radius=3000.0,
        speed=20.0, separation_weight=1.0, cohesion_weight=0.05, goal_weight=2.0,
    )
    overlay = genome_to_overlay(g, base_swarm_count=24, base_behavior="flocking")
    ss = overlay["swarm_spec"]
    assert ss["spawn_radius"] == pytest.approx(3000.0)
    assert ss["speed"] == pytest.approx(20.0)
    assert len(ss["spawn_arc_deg"]) == 2


def test_genome_to_overlay_arc_calculation():
    g = Genome(
        spawn_arc_center_deg=180.0, spawn_arc_width_deg=60.0, spawn_radius=4000.0,
        speed=15.0, separation_weight=1.0, cohesion_weight=0.05, goal_weight=2.0,
    )
    overlay = genome_to_overlay(g, base_swarm_count=12, base_behavior="direct")
    arc = overlay["swarm_spec"]["spawn_arc_deg"]
    assert arc[0] == pytest.approx(150.0)
    assert arc[1] == pytest.approx(210.0)


def test_random_genome_respects_bounds():
    rng = np.random.default_rng(42)
    for _ in range(20):
        g = random_genome(rng)
        for attr, (lo, hi) in BOUNDS.items():
            val = getattr(g, attr)
            assert lo <= val <= hi, f"{attr}={val} out of [{lo},{hi}]"


def test_genome_to_overlay_direct_behavior_has_no_kinematics():
    g = Genome(
        spawn_arc_center_deg=0, spawn_arc_width_deg=90, spawn_radius=3000,
        speed=20, separation_weight=1, cohesion_weight=0.05, goal_weight=2,
    )
    overlay = genome_to_overlay(g, base_swarm_count=10, base_behavior="direct")
    assert "kinematics" not in overlay


def test_run_ga_returns_result():
    from beam.evolve.ga import GAConfig, run_ga
    from beam.engine.loop import LoopConfig
    # Very small config for speed: 4 drones, 2 gens, 4 individuals
    cfg = GAConfig(
        base_scenario="swarm_24",
        defender_solver="greedy_urgent",
        seed=42,
        population_size=4,
        generations=2,
        mutation_rate=0.2,
        crossover_rate=0.7,
        elite_count=1,
        base_swarm_count=4,
        base_behavior="direct",
    )
    loop_cfg = LoopConfig(max_epochs=5)
    result = run_ga(cfg, loop_cfg=loop_cfg)
    assert result.best_genome is not None
    assert result.best_fitness >= 0.0
    assert len(result.generation_best) == 2
