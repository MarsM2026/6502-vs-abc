"""Window CGP directly on an NMOS cell netlist (see cells.py).

windows.py evolves two-input gates and needs ABC to map the result onto NMOS cells;
ABC's restructuring then undoes most of what evolution found (docs/real6502.md). Here
the genome's nodes *are* library cells (INV, NAND2-4, NOR2-8, AOI/OAI) and the size
measure is their exact transistor count, so every accepted replacement lowers the
transistor count of the final netlist by exactly that much. No re-mapping afterwards.

Per window (connected cells, at most K external inputs):
  1. truth table of the window outputs over the reachable input combinations
     (SAT don't-cares on the two-input expansion, dontcare.py);
  2. (1 + 4) ES, seeded with the window's own cells: must stay correct on every
     reachable combination, then fewer transistors wins;
  3. splice back if strictly cheaper (rejected if it would create a loop).

Usage (from the project root):
  python src/cell_windows.py --start build/cpu_abc_x10.json --script "+strash;dc2;&get,-n;&dch,-f;&put;amap,-F,8,-A,16"
  python src/cell_windows.py --cells build/cells_opt.json        # continue from a cell netlist
then validate the expansion:  python src/csim.py --netlist build/cells_opt_gates.json
"""

from __future__ import annotations

import argparse
import functools
import random
import time
from dataclasses import dataclass
from pathlib import Path

from cells import CELLS, CellInst, CellNet, expand, load_cells, map_netlist, save_cells, topo_sort
from dontcare import reachable_combos
from netlist import save_circuit

ROOT = Path(__file__).resolve().parent.parent
LIB = sorted(CELLS.values(), key=lambda c: c.name)
MAXA = max(c.arity for c in LIB)
LIB_INDEX = {c.name: i for i, c in enumerate(LIB)}
ARITY = [c.arity for c in LIB]


@dataclass
class CGenome:
    n_inputs: int
    funcs: list[int]
    ins: list[list[int]]      # MAXA addresses per node; only the first `arity` are used
    outputs: list[int]

    def copy(self) -> "CGenome":
        return CGenome(self.n_inputs, list(self.funcs), [list(x) for x in self.ins], list(self.outputs))

    def active(self) -> list[int]:
        """Nodes that an output depends on (one backward sweep; nodes only read earlier ones)."""
        n_in, n = self.n_inputs, len(self.funcs)
        mark = [False] * n
        for o in self.outputs:
            if o >= n_in:
                mark[o - n_in] = True
        for i in range(n - 1, -1, -1):
            if mark[i]:
                for a in self.ins[i][:ARITY[self.funcs[i]]]:
                    if a >= n_in:
                        mark[a - n_in] = True
        return [i for i in range(n) if mark[i]]

    def area(self, act: list[int] | None = None) -> int:
        return sum(LIB[self.funcs[i]].area for i in (self.active() if act is None else act))

    def simulate(self, pats: list[int], mask: int, act: list[int] | None = None) -> list[int]:
        vals = list(pats) + [0] * len(self.funcs)
        n_in = self.n_inputs
        for i in (self.active() if act is None else act):
            cell = LIB[self.funcs[i]]
            vals[n_in + i] = cell.fn([vals[a] for a in self.ins[i][:cell.arity]], mask)
        return [vals[o] for o in self.outputs]


def mutate(g: CGenome, rng: random.Random, act: set[int] | None = None) -> CGenome:
    """Mutate random genes until one *active* gene has changed (as cgp.mutate_single_active).
    `act`: the parent's active nodes, if already known."""
    c = g.copy()
    act = set(c.active()) if act is None else act
    n_nodes = len(c.funcs)
    while True:
        k = rng.randrange(n_nodes + len(c.outputs))
        if k >= n_nodes:
            c.outputs[k - n_nodes] = rng.randrange(c.n_inputs + n_nodes)
            return c
        arity = LIB[c.funcs[k]].arity
        gene = rng.randrange(arity + 1)
        if gene == 0:
            c.funcs[k] = rng.randrange(len(LIB))
        else:
            c.ins[k][gene - 1] = rng.randrange(c.n_inputs + k)
        if k in act:
            return c


def fitness(g: CGenome, pats: list[int], want: list[int], mask: int) -> tuple[int, int]:
    act = g.active()
    wrong = sum((p ^ w).bit_count() for p, w in zip(g.simulate(pats, mask, act), want))
    return -wrong, (-g.area(act) if wrong == 0 else 0)


def evolve(seed: CGenome, pats: list[int], want: list[int], mask: int, gens: int,
           rng: random.Random, lam: int = 4) -> CGenome:
    parent, pk = seed, fitness(seed, pats, want, mask)
    pact = set(parent.active())
    for _ in range(gens):
        best, bk = None, None
        for _ in range(lam):
            ch = mutate(parent, rng, pact)
            k = fitness(ch, pats, want, mask)
            if bk is None or k >= bk:
                best, bk = ch, k
        if bk >= pk:
            if best is not parent:
                pact = set(best.active())
            parent, pk = best, bk
    return parent if pk[0] == 0 else seed


def _inputs_of(cn: CellNet, members: set[int]) -> set[int]:
    produced = {cn.cells[i].y for i in members}
    return {s for i in members for s in cn.cells[i].ins if s not in produced}


def grow_window(cn: CellNet, root: int, driver: dict[int, int], k: int, max_cells: int,
                rng: random.Random) -> set[int]:
    members = {root}
    while len(members) < max_cells:
        cands = {driver[s] for i in members for s in cn.cells[i].ins if s in driver} - members
        best, best_n = [], None
        for j in cands:
            n = len(_inputs_of(cn, members | {j}))
            if n <= k and (best_n is None or n < best_n):
                best, best_n = [j], n
            elif n == best_n:
                best.append(j)
        if not best:
            break
        members.add(rng.choice(best))
    return members


def window_problem(cn: CellNet, members: set[int], outs_needed: set[int], combos):
    order = sorted(members)
    base = sorted(_inputs_of(cn, members))
    ins = base + [s for s in (0, 1) if s not in base]
    outs = [cn.cells[i].y for i in order if cn.cells[i].y in outs_needed]
    n_vec = (1 << len(base)) if combos is None else len(combos)
    mask = (1 << n_vec) - 1
    pats = []
    for j, s in enumerate(ins):
        if j >= len(base) or s in (0, 1):
            pats.append(mask if s == 1 else 0)
        elif combos is not None:
            pats.append(sum(1 << v for v, cmb in enumerate(combos) if (cmb >> j) & 1))
        else:
            pats.append(sum(1 << v for v in range(n_vec) if (v >> j) & 1))
    val = dict(zip(ins, pats))
    for i in order:
        c = cn.cells[i]
        val[c.y] = CELLS[c.type].fn([val[s] for s in c.ins], mask)
    return ins, outs, pats, [val[s] for s in outs], mask


def window_genome(cn: CellNet, members: set[int], ins: list[int], outs: list[int], extra: int,
                  rng: random.Random) -> CGenome:
    order = sorted(members)
    addr = {s: j for j, s in enumerate(ins)}
    funcs, gins = [], []
    for node, i in enumerate(order):
        c = cn.cells[i]
        a = [addr[s] for s in c.ins]
        funcs.append(LIB_INDEX[c.type])
        gins.append(a + [a[0]] * (MAXA - len(a)))
        addr[c.y] = len(ins) + node
    for node in range(len(order), len(order) + extra):
        funcs.append(rng.randrange(len(LIB)))
        gins.append([rng.randrange(len(ins) + node) for _ in range(MAXA)])
    return CGenome(len(ins), funcs, gins, [addr[s] for s in outs])


def splice(cn: CellNet, members: set[int], ins: list[int], outs: list[int], g: CGenome) -> bool:
    saved = (list(cn.cells), list(cn.dffs), {k: list(v) for k, v in cn.outputs.items()}, cn.n_signals)
    addr = {j: s for j, s in enumerate(ins)}
    new = []
    for i in g.active():
        cell = LIB[g.funcs[i]]
        y = cn.n_signals
        cn.n_signals += 1
        new.append(CellInst(cell.name, [addr[a] for a in g.ins[i][:cell.arity]], y))
        addr[g.n_inputs + i] = y
    mapping = {o: addr[g.outputs[k]] for k, o in enumerate(outs)}
    r = lambda s: mapping.get(s, s)  # noqa: E731
    cells = [c for i, c in enumerate(cn.cells) if i not in members] + new
    cn.cells = [CellInst(c.type, [r(s) for s in c.ins], c.y) for c in cells]
    cn.dffs = [(r(d), q) for d, q in cn.dffs]
    cn.outputs = {n: [r(s) for s in b] for n, b in cn.outputs.items()}
    sweep(cn)
    try:
        topo_sort(cn)
        return True
    except ValueError as e:
        if "combinational loop" not in str(e):
            raise
        cn.cells, cn.dffs, cn.outputs, cn.n_signals = saved
        return False


def sweep(cn: CellNet) -> None:
    while True:
        used = {s for b in cn.outputs.values() for s in b} | {d for d, _ in cn.dffs}
        used |= {s for c in cn.cells for s in c.ins}
        kept = [c for c in cn.cells if c.y in used]
        if len(kept) == len(cn.cells):
            return
        cn.cells = kept


def optimise(cn: CellNet, passes: int, k: int, max_cells: int, gens: int, rng: random.Random,
             dont_cares: bool = True, max_windows: int | None = None, log=print) -> None:
    for p in range(1, passes + 1):
        t0, start = time.perf_counter(), cn.area
        tried = improved = loops = with_dc = 0
        visited: set[int] = set()
        circ = expand(cn) if dont_cares else None
        gdriver = {g.y: i for i, g in enumerate(circ.gates)} if circ else None
        while max_windows is None or tried < max_windows:
            driver = {c.y: i for i, c in enumerate(cn.cells)}
            roots = [i for i, c in enumerate(cn.cells) if c.y not in visited]
            if not roots:
                break
            root = rng.choice(roots)
            visited.add(cn.cells[root].y)
            members = grow_window(cn, root, driver, k, max_cells, rng)
            if len(members) < 2:
                continue
            produced = {cn.cells[i].y for i in members}
            outside = {s for b in cn.outputs.values() for s in b} | {d for d, _ in cn.dffs}
            outside |= {s for i, c in enumerate(cn.cells) if i not in members for s in c.ins}
            combos = None
            if dont_cares:
                combos = reachable_combos(circ, sorted(_inputs_of(cn, members)), gdriver)
                with_dc += combos is not None
            ins, outs, pats, want, mask = window_problem(cn, members, outside & produced, combos)
            seed = window_genome(cn, members, ins, outs, len(members) + 10, rng)
            before = seed.area()
            best = evolve(seed, pats, want, mask, gens, rng)
            tried += 1
            if best.area() < before:
                if splice(cn, members, ins, outs, best):
                    improved += 1
                    if dont_cares:
                        circ = expand(cn)
                        gdriver = {g.y: i for i, g in enumerate(circ.gates)}
                else:
                    loops += 1
            if tried % 50 == 0:
                log(f"  pass {p}: {tried} windows ({with_dc} with don't-cares), {improved} improved, "
                    f"transistors {cn.area} ({time.perf_counter() - t0:.0f}s)")
        log(f"pass {p} done: {start} -> {cn.area} transistors ({tried} windows, {with_dc} with don't-cares, "
            f"{improved} improved, {loops} rejected for loops, {time.perf_counter() - t0:.0f}s)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--start", type=Path, help="gate netlist to map with ABC first")
    src.add_argument("--cells", type=Path, help="cell netlist from an earlier run")
    ap.add_argument("--script", default=None, help="ABC script for --start (nmos_map syntax)")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "cells_opt.json")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--max-cells", type=int, default=20)
    ap.add_argument("--gens", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-windows", type=int, default=None)
    ap.add_argument("--no-dont-cares", action="store_true")
    args = ap.parse_args(argv)
    cn = map_netlist(args.start, args.script) if args.start else load_cells(args.cells)
    log = functools.partial(print, flush=True)
    log("start:", cn.summary())
    log(f"settings: k={args.k} max_cells={args.max_cells} gens={args.gens} dont_cares={not args.no_dont_cares}")
    optimise(cn, args.passes, args.k, args.max_cells, args.gens, random.Random(args.seed),
             dont_cares=not args.no_dont_cares, max_windows=args.max_windows, log=log)
    log("final:", cn.summary())
    save_cells(cn, args.out)
    gates = args.out.with_name(args.out.stem + "_gates.json")
    save_circuit(expand(cn), gates)
    log(f"saved {args.out} and {gates}; validate with: python src/csim.py --netlist {gates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
