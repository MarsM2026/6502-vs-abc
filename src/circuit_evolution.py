"""(1 + lambda) evolution strategy over CGP circuits.

Each generation: one parent produces ``lam`` mutated children; the best child
replaces the parent if it is at least as good (ties accepted -> neutral drift).
This is the standard CGP search; it is still a genetic algorithm (population of
1 + lambda, truncation selection), just without crossover, which empirically
helps little for CGP netlists.

Fitness is lexicographic:
  1. number of correct output bits over the full truth table (correctness)
  2. fewer active gates (area), which only matters among equally-correct circuits
So once a circuit is 100% correct it can never become incorrect again; from
then on evolution only searches for smaller circuits.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from cgp import Genome, mutate_single_active, random_genome
from targets import Target


def score(g: Genome, t: Target) -> int:
    """Number of correct output bits across all input vectors."""
    produced = g.simulate(t.input_patterns, t.mask)
    wrong = sum((p ^ e).bit_count() for p, e in zip(produced, t.output_patterns))
    return t.max_score - wrong


def fitness_key(g: Genome, t: Target, cost: Callable[[Genome], int] | None = None) -> tuple[int, int]:
    """Two-phase fitness. Gate count only counts once the circuit is fully correct:
    penalising size earlier starves the search of the extra gates it needs to grow
    (observed: random adder4 runs stuck at 86% with only 13-20 gates)."""
    s = score(g, t)
    if s != t.max_score:
        return s, 0
    return s, -(cost(g) if cost else g.gate_count())


@dataclass
class Progress:
    generation: int
    evaluations: int
    score: int
    max_score: int
    gates: int
    elapsed: float

    @property
    def correct(self) -> bool:
        return self.score == self.max_score


@dataclass
class Result:
    best: Genome
    history: list[Progress]
    first_correct_generation: int | None


def evolve(
    target: Target,
    n_nodes: int = 100,
    lam: int = 4,
    generations: int = 100_000,
    rng: random.Random | None = None,
    initial: Genome | None = None,
    stop_when_correct: bool = False,
    on_progress: Callable[[Progress], None] | None = None,
    report_every: int = 5_000,
    cost: Callable[[Genome], int] | None = None,
) -> Result:
    """`cost` replaces the gate count as the size measure (e.g. nmos_cost.genome_cost)."""
    rng = rng or random.Random()
    start = time.perf_counter()
    parent = initial.copy() if initial else random_genome(target.n_inputs, target.n_outputs, n_nodes, rng)
    parent_key = fitness_key(parent, target, cost)
    evaluations = 1
    history: list[Progress] = []
    first_correct = 0 if parent_key[0] == target.max_score else None

    def record(gen: int) -> None:
        p = Progress(gen, evaluations, parent_key[0], target.max_score, parent.gate_count(),
                     time.perf_counter() - start)
        history.append(p)
        if on_progress:
            on_progress(p)

    record(0)
    gen = 0
    for gen in range(1, generations + 1):
        best_child, best_key = None, None
        for _ in range(lam):
            child = mutate_single_active(parent, rng)
            key = fitness_key(child, target, cost)
            evaluations += 1
            if best_key is None or key >= best_key:
                best_child, best_key = child, key
        improved = best_key > parent_key
        if best_key >= parent_key:
            parent, parent_key = best_child, best_key
        if improved:
            if first_correct is None and parent_key[0] == target.max_score:
                first_correct = gen
            record(gen)
            if stop_when_correct and first_correct is not None:
                break
        elif gen % report_every == 0:
            record(gen)
    if history[-1].generation != gen:
        record(gen)
    return Result(parent, history, first_correct)
