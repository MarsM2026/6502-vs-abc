"""Window-based CGP optimisation of a large sequential gate-level netlist.

The 6502 netlist is too entangled to cut at its flip-flops (most cones depend on
>20 sources and 761 gates are shared by >10 sinks). Instead we optimise small
*windows*: a connected set of gates with at most K external inputs.

For each window:
  1. compute its exact truth table over all 2^K input combinations (bit-parallel);
  2. seed a CGP genome with the window's own gates and let evolution shrink it
     (two-phase fitness: must stay 100% correct, fewer gates wins);
  3. if the result is smaller, splice it back in.
A replacement that matches the window on *every* input combination cannot change
the behaviour of the whole circuit, so correctness holds by construction. With
--dont-cares, input combinations that can never occur (found with SAT, see
dontcare.py) are left out of the truth table.

Usage (from the project root):
  python src/windows.py                          # default: 2 passes, K=10
  python src/windows.py --passes 3 --k 12 --gens 3000
then validate:  python src/csim.py --netlist build/cpu_opt.json
"""

from __future__ import annotations

import argparse
import copy
import functools
import random
import time
from pathlib import Path

from dontcare import reachable_combos
from invariants import load_invariants
from nmos_cost import circuit_cost, genome_cost
from cgp import GATE_INDEX, GATES, Genome, random_genome
from circuit_evolution import evolve
from netlist import Circuit, Gate, cleanup, load_circuit, resort, save_circuit, substitute, sweep
from targets import Target

ROOT = Path(__file__).resolve().parent.parent
_OPS = {
    "AND": lambda a, b, m: a & b, "OR": lambda a, b, m: a | b, "XOR": lambda a, b, m: a ^ b,
    "NAND": lambda a, b, m: ~(a & b) & m, "NOR": lambda a, b, m: ~(a | b) & m,
    "XNOR": lambda a, b, m: ~(a ^ b) & m, "NOT": lambda a, b, m: ~a & m,
}


def _inputs_of(gates: list[Gate], members: set[int]) -> set[int]:
    produced = {gates[i].y for i in members}
    ins = set()
    for i in members:
        g = gates[i]
        for s in ((g.a,) if g.op == "NOT" else (g.a, g.b)):
            if s not in produced:
                ins.add(s)
    return ins


def grow_window(c: Circuit, root: int, driver: dict[int, int], k: int, max_gates: int,
                rng: random.Random) -> set[int]:
    """Greedy backwards growth from `root`: repeatedly add the fan-in gate that keeps
    the number of external inputs smallest, while it stays <= k."""
    members = {root}
    while len(members) < max_gates:
        cands = set()
        for i in members:
            g = c.gates[i]
            for s in (g.a, g.b):
                j = driver.get(s)
                if j is not None and j not in members:
                    cands.add(j)
        best, best_n = [], None
        for j in cands:
            n = len(_inputs_of(c.gates, members | {j}))
            if n <= k and (best_n is None or n < best_n):
                best, best_n = [j], n
            elif n == best_n:
                best.append(j)
        if not best:
            break
        members.add(rng.choice(best))
    return members


def window_target(c: Circuit, members: set[int], users_outside: set[int],
                  combos: list[int] | None = None):
    """Returns (ordered input signals, ordered output signals, Target).
    With `combos` (reachable input combinations from dontcare.py) the target only
    contains those vectors; otherwise it is exhaustive over all 2^k."""
    order = sorted(members)  # circuit.gates is topologically sorted
    base = sorted(_inputs_of(c.gates, members))
    # Constants 0 and 1 are always offered as extra inputs: with don't-cares an
    # output is often constant on every reachable combination.
    ins = base + [s for s in (0, 1) if s not in base]
    outs = [c.gates[i].y for i in order if c.gates[i].y in users_outside]
    n = len(ins)
    n_vec = (1 << len(base)) if combos is None else len(combos)
    mask = (1 << n_vec) - 1
    pats = []
    for j, s in enumerate(ins):
        if j >= len(base) or s in (0, 1):
            pats.append(mask if s == 1 else 0)
        elif combos is not None:
            pats.append(sum(1 << v for v, cmb in enumerate(combos) if (cmb >> j) & 1))
        else:
            p = 0
            for v in range(n_vec):
                if (v >> j) & 1:
                    p |= 1 << v
            pats.append(p)
    val = dict(zip(ins, pats))
    for i in order:
        g = c.gates[i]
        val[g.y] = _OPS[g.op](val[g.a], val[g.b], mask)
    t = Target("window", [f"i{j}" for j in range(n)], [f"o{j}" for j in range(len(outs))],
               pats, [val[s] for s in outs], mask, n_vec)
    return ins, outs, t


def window_genome(c: Circuit, members: set[int], ins: list[int], outs: list[int],
                  n_nodes: int, rng: random.Random) -> Genome:
    order = sorted(members)
    addr = {s: j for j, s in enumerate(ins)}
    funcs, in1, in2 = [], [], []
    for node, i in enumerate(order):
        g = c.gates[i]
        funcs.append(GATE_INDEX[g.op])
        in1.append(addr[g.a])
        in2.append(addr[g.a] if g.op == "NOT" else addr[g.b])
        addr[g.y] = len(ins) + node
    pad = random_genome(len(ins), 0, n_nodes, rng)
    used = len(order)
    return Genome(len(ins), funcs + pad.funcs[used:], in1 + pad.in1[used:], in2 + pad.in2[used:],
                  [addr[s] for s in outs])


def splice(c: Circuit, members: set[int], ins: list[int], outs: list[int], best: Genome) -> bool:
    """Replace the window by `best`. Returns False (circuit unchanged) if that would
    create a combinational loop. That can happen when the window is non-convex: a
    window output feeds outside logic that comes back in as a window input, and the
    evolved circuit wires that input into the output's own logic."""
    saved = (copy.deepcopy(c.gates), list(c.dffs), {k: list(v) for k, v in c.outputs.items()}, c.n_signals)
    try:
        _splice(c, members, ins, outs, best)
        return True
    except ValueError as e:
        if "combinational loop" not in str(e):
            raise
        c.gates, c.dffs, c.outputs, c.n_signals = saved
        return False


def _splice(c: Circuit, members: set[int], ins: list[int], outs: list[int], best: Genome) -> None:
    addr_sig = {j: s for j, s in enumerate(ins)}
    new_gates = []
    for i in best.active_nodes():
        y = c.n_signals
        c.n_signals += 1
        op = GATES[best.funcs[i]][0]
        a = addr_sig[best.in1[i]]
        b = a if op == "NOT" else addr_sig[best.in2[i]]
        new_gates.append(Gate(op, a, b, y))
        addr_sig[best.n_inputs + i] = y
    mapping = {o: addr_sig[best.outputs[k]] for k, o in enumerate(outs)}
    c.gates = [g for i, g in enumerate(c.gates) if i not in members] + new_gates
    substitute(c, mapping)
    sweep(c)
    resort(c)


def optimise(c: Circuit, passes: int, k: int, max_gates: int, gens: int, rng: random.Random,
             log=print, max_windows: int | None = None, dont_cares: bool = False,
             invariants=None, cost=None) -> None:
    """`cost`: size measure for a window genome (default: gate count)."""
    measure = cost or (lambda g: g.gate_count())
    for p in range(1, passes + 1):
        t0 = time.perf_counter()
        start_count = c.gate_count
        tried = improved = with_dc = loops = 0
        visited: set[int] = set()  # output signals of gates already used as a root this pass
        while True:
            driver = {g.y: i for i, g in enumerate(c.gates)}
            roots = [i for i, g in enumerate(c.gates) if g.y not in visited]
            if not roots or (max_windows is not None and tried >= max_windows):
                break
            root = rng.choice(roots)
            visited.add(c.gates[root].y)
            members = grow_window(c, root, driver, k, max_gates, rng)
            if len(members) < 3:
                continue
            produced = {c.gates[i].y for i in members}
            outside = {s for bits in c.outputs.values() for s in bits} | {d for d, _ in c.dffs}
            for i, g in enumerate(c.gates):
                if i not in members:
                    outside.add(g.a)
                    outside.add(g.b)
            combos = None
            if dont_cares:
                combos = reachable_combos(c, sorted(_inputs_of(c.gates, members)), driver,
                                          invariants=invariants)
                with_dc += combos is not None
            ins, outs, target = window_target(c, members, outside & produced, combos)
            seed = window_genome(c, members, ins, outs, len(members) + 20, rng)
            before = measure(seed)
            res = evolve(target, lam=4, generations=gens, rng=rng, initial=seed, cost=cost)
            tried += 1
            if res.history[-1].correct and measure(res.best) < before:
                # New gates may become roots later; this terminates because every
                # successful splice strictly lowers the gate count (or, with a cost
                # function, the whole circuit's cost, checked below).
                if cost is not None:
                    saved = (copy.deepcopy(c.gates), list(c.dffs),
                             {k: list(v) for k, v in c.outputs.items()}, c.n_signals)
                    old_total = circuit_cost(c)
                if splice(c, members, ins, outs, res.best):
                    if cost is not None and circuit_cost(c) >= old_total:
                        c.gates, c.dffs, c.outputs, c.n_signals = saved   # local gain, global loss
                    else:
                        improved += 1
                else:
                    loops += 1
            if tried % 50 == 0:
                log(f"  pass {p}: {tried} windows ({with_dc} with don't-cares), {improved} improved, gates {c.gate_count} "
                    f"({time.perf_counter() - t0:.0f}s)")
        log(f"pass {p} done: {start_count} -> {c.gate_count} gates "
            f"({tried} windows, {with_dc} with don't-cares, {improved} improved, {loops} rejected for loops, "
            f"{time.perf_counter() - t0:.0f}s)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Window-based CGP optimisation of the 6502 netlist.")
    ap.add_argument("--netlist", type=Path, default=ROOT / "build" / "cpu_gates.json")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "cpu_opt.json")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--k", type=int, default=10, help="max window inputs (default 10)")
    ap.add_argument("--max-gates", type=int, default=25, help="max gates per window (default 25)")
    ap.add_argument("--gens", type=int, default=2000, help="CGP generations per window (default 2000)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-windows", type=int, default=None, help="stop each pass after this many windows")
    ap.add_argument("--dont-cares", action="store_true",
                    help="use SAT to find unreachable window-input combinations and ignore them")
    ap.add_argument("--cost", choices=["gates", "nmos"], default="gates",
                    help="what to minimise: two-input gates, or estimated NMOS transistors")
    ap.add_argument("--invariants", type=Path, default=None,
                    help="register-state invariants (from invariants.py) for sequential don't-cares; implies --dont-cares")
    args = ap.parse_args(argv)
    inv = load_invariants(args.invariants) if args.invariants else None
    if inv is not None:
        args.dont_cares = True

    rng = random.Random(args.seed)
    c = load_circuit(args.netlist)
    print("start:        ", c.summary())
    cleanup(c)
    print("after cleanup:", c.summary())
    print(f"estimated NMOS transistors (combinational, nmos_cost model): {circuit_cost(c)}")
    log = functools.partial(print, flush=True)
    log(f"settings: k={args.k} max_gates={args.max_gates} gens={args.gens} dont_cares={args.dont_cares} "
        f"invariants={len(inv) if inv else 0} cost={args.cost}")
    optimise(c, args.passes, args.k, args.max_gates, args.gens, rng, log=log,
             max_windows=args.max_windows, dont_cares=args.dont_cares, invariants=inv,
             cost=genome_cost if args.cost == "nmos" else None)
    cleanup(c)
    print("final:        ", c.summary())
    print(f"estimated NMOS transistors (combinational, nmos_cost model): {circuit_cost(c)}")
    save_circuit(c, args.out)
    print(f"saved {args.out}; validate with: python src/csim.py --netlist {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
