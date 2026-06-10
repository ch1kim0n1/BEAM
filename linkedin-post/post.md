# LinkedIn Post — BEAM

---

Drone swarms are cheap. The systems that stop them are not. I built a live simulator to make that tradeoff visible.

The targeting problem looks simple: assign lasers to drones. It's not. Each turret must slew to aim (sequence-dependent setup time), then hold the beam on a target long enough to deposit enough energy to kill it (dwell-to-kill). Every drone has a hard deadline — its time-to-impact on the asset. Get the firing order wrong and a drone gets through even when you had enough power to kill it. The general case is NP-complete.

So BEAM races five OR solvers every 500ms decision epoch: two greedy heuristics, an auction algorithm, a metaheuristic, and an exact CP-SAT reference that proves the true optimum. Every frame streams over WebSocket. Every run is deterministic: same seed + solver = SHA-256 identical telemetry. The auction solver runs in 1.9ms and stays within 3.1% of the CP-SAT optimum. You can watch the optimality gap live as the swarm closes in real time.

24 drones, 4 turrets, 21 seconds: 24 kills, 0 leaks, cost breakeven at t=4s, net +$43k.

Stack: Python / Google OR-Tools CP-SAT / FastAPI / WebSocket / TypeScript / Pixi.js (WebGL)

Code on GitHub: github.com/ch1kim0n1/BEAM

#OperationsResearch #DefenseTech #WeaponTargetAssignment #OptimizationAlgorithms #buildinpublic #OpenSource

---

Character count: ~1290
Hashtags: #OperationsResearch #DefenseTech #WeaponTargetAssignment #OptimizationAlgorithms #buildinpublic #OpenSource
