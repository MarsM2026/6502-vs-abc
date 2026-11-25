"""Register-state invariants: facts about the flip-flops that hold in every reachable state.

Window don't-cares (dontcare.py) treat every flip-flop as free, so they only exclude
window-input combinations that are impossible for *any* register state. Many register
states can never be reached from power-on, though (think one-hot state machines). If we
know facts that hold in every reachable state, a window may also ignore combinations
that need an unreachable state.

Method:
  1. Candidates: simulate the circuit bit-parallel with random inputs from the initial
     state (all flip-flops 0, then reset). Every 1- or 2-literal clause over the flip-flop
     outputs that is never violated is a candidate, e.g. (~q5) "q5 is always 0", or
     (~q3 | ~q9) "q3 and q9 are never both 1".
  2. Proof (Houdini): keep only candidates that hold in the initial state, and repeatedly
     drop every candidate that SAT can falsify one clock cycle later, assuming all the
     remaining candidates now and with *all* inputs free (reset, IRQ, NMI, RDY, DI). The
     fixpoint is an inductive invariant: it holds in every state reachable from the
     initial state.

Soundness of using it: if the optimised circuit computes the same next state and
outputs as the original on every state satisfying the invariant, both circuits run in
lockstep from the initial state forever (by induction). The assumption is the initial
state: all flip-flops 0, as in csim.py.

Usage (from the project root):
  python src/invariants.py --netlist build/cpu_round3.json --out build/cpu_round3.inv.json
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from pysat.solvers import Solver

from dontcare import _gate_clauses, _lit
from netlist import Circuit, load_circuit

ROOT = Path(__file__).resolve().parent.parent
_OPS = {
    "AND": lambda a, b, m: a & b, "OR": lambda a, b, m: a | b, "XOR": lambda a, b, m: a ^ b,
    "NAND": lambda a, b, m: ~(a & b) & m, "NOR": lambda a, b, m: ~(a | b) & m,
    "XNOR": lambda a, b, m: ~(a ^ b) & m, "NOT": lambda a, b, m: ~a & m,
}

# A clause is a tuple of literals; a literal is (signal, polarity) with polarity True = "signal is 1".
Clause = tuple


def simulate_states(c: Circuit, width: int, cycles: int, rng: random.Random,
                    reset_cycles: int = 8, input_probs: dict[str, float] | None = None) -> tuple[list[int], int]:
    """Returns, per flip-flop, a bit vector of its value in every simulated state
    (width parallel random runs x cycles). Starts from all-zero, holds reset first."""
    probs = {"IRQ": 0.05, "NMI": 0.02, "RDY": 0.9, "reset": 0.002, "clk": 0.0}
    probs.update(input_probs or {})
    m = (1 << width) - 1

    def rand_bits(p: float) -> int:
        if p <= 0:
            return 0
        if p >= 1:
            return m
        v = 0
        for i in range(width):
            if rng.random() < p:
                v |= 1 << i
        return v

    q = {qs: 0 for _, qs in c.dffs}
    trace = [0] * len(c.dffs)
    shift = 0
    for cyc in range(cycles):
        val = {0: 0, 1: m}
        val.update(q)
        for name, bits in c.inputs.items():
            for s in bits:
                if name == "reset" and cyc < reset_cycles:
                    val[s] = m
                elif name in probs:
                    val[s] = rand_bits(probs[name])
                else:                           # data bus: uniform random
                    val[s] = rng.getrandbits(width)
        if cyc >= reset_cycles:                 # record states after the reset sequence too
            for i, (_, qs) in enumerate(c.dffs):
                trace[i] |= q[qs] << shift
            shift += width
        for g in c.gates:
            val[g.y] = _OPS[g.op](val[g.a], val[g.b], m)
        q = {qs: val[d] for d, qs in c.dffs}
    return trace, shift


def candidates(c: Circuit, trace: list[int], nbits: int) -> list[Clause]:
    """All 1- and 2-literal clauses over flip-flop outputs never violated in the trace
    and true in the all-zero initial state."""
    qs = [q for _, q in c.dffs]
    n = len(qs)
    full = (1 << nbits) - 1
    ones = trace
    zeros = [~t & full for t in trace]
    cands: list[Clause] = []
    for i in range(n):
        if ones[i] == 0:
            cands.append(((qs[i], False),))          # always 0 (true at init)
        # "always 1" is false in the all-zero initial state: skip.
    const = {i for i in range(n) if ones[i] == 0}
    for i in range(n):
        if i in const:
            continue
        for j in range(i + 1, n):
            if j in const:
                continue
            # clause (li | lj) is violated where li and lj are both false.
            for pi in (True, False):
                for pj in (True, False):
                    if pi and pj:
                        continue                     # (qi | qj) is false at all-zero init
                    fi = zeros[i] if pi else ones[i]   # where li is false
                    fj = zeros[j] if pj else ones[j]
                    if fi & fj == 0:
                        cands.append(((qs[i], pi), (qs[j], pj)))
    return cands


def prove(c: Circuit, cands: list[Clause], log=print) -> list[Clause]:
    """Houdini: greatest subset of `cands` that is inductive over one clock cycle."""
    d_of = {q: d for d, q in c.dffs}
    alive = set(range(len(cands)))
    with Solver(name="g4") as s:
        s.add_clause([_lit(0, False)])
        s.add_clause([_lit(1)])
        for g in c.gates:
            for cl in _gate_clauses(g.op, g.y, g.a, g.b):
                s.add_clause(cl)
        top = c.n_signals + 1
        act = []                                    # activation var per candidate (current state)
        for k, cl in enumerate(cands):
            top += 1
            act.append(top)
            s.add_clause([-top] + [_lit(sig, pol) for sig, pol in cl])
        rounds = 0
        while True:
            rounds += 1
            killed = 0
            for k in sorted(alive):
                if k not in alive:
                    continue
                assum = [act[j] for j in alive]
                # negation of candidate k on the next state: every literal false at D
                assum += [_lit(d_of[sig], not pol) for sig, pol in cands[k]]
                if s.solve(assumptions=assum):
                    model = s.get_model()
                    def val(sig):
                        return model[sig] > 0 if sig < len(model) else False
                    for j in list(alive):     # kill everything this successor state violates
                        if not any(val(d_of[sig]) == pol for sig, pol in cands[j]):
                            alive.discard(j)
                            killed += 1
            log(f"  round {rounds}: removed {killed}, {len(alive)} remain")
            if killed == 0:
                break
    return [cands[k] for k in sorted(alive)]


def save_invariants(inv: list[Clause], path: Path) -> None:
    path.write_text(json.dumps([[[sig, pol] for sig, pol in cl] for cl in inv]))


def load_invariants(path: Path) -> list[Clause]:
    return [tuple((sig, pol) for sig, pol in cl) for cl in json.loads(Path(path).read_text())]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--width", type=int, default=256, help="parallel random runs")
    ap.add_argument("--cycles", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    c = load_circuit(args.netlist)
    print("netlist:", c.summary())
    t0 = time.perf_counter()
    trace, nbits = simulate_states(c, args.width, args.cycles, random.Random(args.seed))
    cands = candidates(c, trace, nbits)
    print(f"{len(cands)} candidates from {args.width}x{args.cycles} random cycles "
          f"({time.perf_counter() - t0:.0f}s)", flush=True)
    t0 = time.perf_counter()
    inv = prove(c, cands, log=lambda *a: print(*a, flush=True))
    units = sum(len(cl) == 1 for cl in inv)
    print(f"proved {len(inv)} invariants ({units} constant flip-flops, {len(inv) - units} two-literal) "
          f"({time.perf_counter() - t0:.0f}s)")
    save_invariants(inv, args.out)
    print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
