"""Satisfiability don't-cares for a window, computed with SAT.

The inputs of a window are themselves functions of the circuit's *sources*
(primary inputs and flip-flop outputs). Some combinations of window-input values
can never occur, whatever the sources are; the window's output on those
combinations is irrelevant ("don't care").

We enumerate the combinations that *can* occur (AllSAT projected onto the window
inputs) over the transitive fan-in cone of the window inputs, with all sources
left free. A replacement that matches the original on every reachable
combination therefore leaves every sink (flip-flop D input, primary output)
computing exactly the same function of the sources, so the CPU is unchanged for
every possible state and input, not just the ones a test happens to visit.
"""

from __future__ import annotations

from pysat.solvers import Solver

from netlist import Circuit


def _lit(sig: int, positive: bool = True) -> int:
    return (sig + 1) if positive else -(sig + 1)


def _gate_clauses(op: str, y: int, a: int, b: int) -> list[list[int]]:
    inv = op in ("NAND", "NOR", "XNOR")
    Y, nY = (_lit(y, False), _lit(y)) if inv else (_lit(y), _lit(y, False))
    A, nA, B, nB = _lit(a), _lit(a, False), _lit(b), _lit(b, False)
    if op == "NOT":
        return [[_lit(y), A], [_lit(y, False), nA]]
    base = {"NAND": "AND", "NOR": "OR", "XNOR": "XOR"}.get(op, op)
    if base == "AND":
        return [[nY, A], [nY, B], [Y, nA, nB]]
    if base == "OR":
        return [[Y, nA], [Y, nB], [nY, A, B]]
    return [[nY, A, B], [nY, nA, nB], [Y, nA, B], [Y, A, nB]]  # XOR


def reachable_combos(c: Circuit, ins: list[int], driver: dict[int, int] | None = None,
                     limit: int | None = None, invariants=None) -> list[int] | None:
    """All value combinations of `ins` that some source assignment can produce.
    Combination bit j is the value of ins[j]. Returns None when every one of the
    2^k combinations is reachable (no don't-cares) or when more than `limit`
    are found. `invariants` (from invariants.py) are clauses over flip-flop outputs
    that hold in every reachable state; they restrict the sources further."""
    if driver is None:
        driver = {g.y: i for i, g in enumerate(c.gates)}
    k = len(ins)
    full = 1 << k
    limit = full if limit is None else min(limit, full)

    cone, stack = set(), [s for s in ins if s in driver]
    while stack:
        i = driver[stack.pop()]
        if i in cone:
            continue
        cone.add(i)
        g = c.gates[i]
        for s in (g.a, g.b):
            if s in driver and driver[s] not in cone:
                stack.append(s)

    with Solver(name="g4") as solver:
        solver.add_clause([_lit(0, False)])
        solver.add_clause([_lit(1)])
        for i in cone:
            g = c.gates[i]
            for cl in _gate_clauses(g.op, g.y, g.a, g.b):
                solver.add_clause(cl)
        for cl in invariants or ():
            solver.add_clause([_lit(sig, pol) for sig, pol in cl])
        found: list[int] = []
        while solver.solve():
            model = solver.get_model()
            combo = 0
            block = []
            for j, s in enumerate(ins):
                v = model[s] > 0 if s < len(model) else False  # model[s] is literal of var s+1
                if v:
                    combo |= 1 << j
                block.append(_lit(s, not v))
            found.append(combo)
            if len(found) >= limit:
                return None
            solver.add_clause(block)
        return sorted(found)
