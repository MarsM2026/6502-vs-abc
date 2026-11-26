"""Pass-transistor buses and bus-sharing latches in a mapped NMOS cell netlist.

Two techniques of the real 6502 that a static cell library cannot express, because whether
they are correct depends on which states the chip can reach, not on a cell's function:

1. Pass-transistor bus. An AND-OR-INVERT cell !(s1&d1 | s2&d2 | ...) can be built as one
   pass transistor per branch onto a shared node, if at most one select s_i is ever 1 at a
   time (two enabled drivers would fight). We prove that with SAT: the netlist's clauses plus
   its register invariants (src/invariants.py, which hold in every reachable state), with
   all primary inputs free. For each branch we try both literals as the select. The cell
   keeps its function and gets the type P<cell> (or P<cell>X when exactly one select is
   always 1), with the cost from nmos_map.BUS.

2. Latch-input multiplexer. A MUX2I whose only load is one flip-flop's D input moves into
   that flip-flop's master stage (type LMUX2I, cost 1, see nmos_map.LATCH). Its two data
   inputs must already be available inverted, and the gated clocks cost 6 per select signal,
   so we only do it for select signals shared by at least 3 such muxes.

Both rewrites keep every signal's function, so the rewritten netlist simulates exactly like
the input; Klaus Dormann's test on the expansion is a check of the bookkeeping, not of the
proof. The proof assumes the usual two-phase discipline: selects and data are stable while
the clock phase that uses them is active, as in the real chip.

Usage (from the project root):
  python src/bus_rewrite.py --cells build/cpu_pass.json --inv build/cpu_pass.inv.json \
      --out build/cpu_bus.json
"""

from __future__ import annotations

import argparse
import itertools
from pathlib import Path

from pysat.solvers import Solver

from cells import ALL, CellNet, _TREES, expand, load_cells, save_cells
from dontcare import _gate_clauses, _lit
from invariants import load_invariants
from netlist import Circuit, save_circuit
from nmos_map import BUS, DFF_TRANSISTORS, latch_clock_cost, mux_select_cost


class StateOracle:
    """Answers questions about signal values in reachable states (sound, not complete)."""

    def __init__(self, c: Circuit, invariants: list | None = None):
        self.s = Solver(name="g4")
        self.s.add_clause([_lit(0, False)])
        self.s.add_clause([_lit(1)])
        for g in c.gates:
            for cl in _gate_clauses(g.op, g.y, g.a, g.b):
                self.s.add_clause(cl)
        for cl in invariants or []:
            self.s.add_clause([_lit(sig, pol) for sig, pol in cl])
        self._excl: dict[tuple[int, int], bool] = {}

    def exclusive(self, x: int, y: int) -> bool:
        """x and y are never both 1."""
        if x == y:
            return x == 0
        key = (min(x, y), max(x, y))
        if key not in self._excl:
            self._excl[key] = not self.s.solve(assumptions=[_lit(x), _lit(y)])
        return self._excl[key]

    def one_always(self, xs: list[int]) -> bool:
        """At least one of xs is 1."""
        return not self.s.solve(assumptions=[_lit(x, False) for x in xs])

    def close(self) -> None:
        self.s.delete()


def _branches(cell_type: str, ins: list[int]) -> list[list[int]] | None:
    t = _TREES[cell_type]
    if not (isinstance(t, tuple) and t[0] == "|"):
        return None
    out = []
    for k in t[1]:
        if isinstance(k, int):
            out.append([ins[k]])
        elif k[0] == "&" and len(k[1]) == 2 and all(isinstance(x, int) for x in k[1]):
            out.append([ins[x] for x in k[1]])
        else:
            return None
    return out


def choose_selects(branches: list[list[int]], oracle: StateOracle) -> tuple[list[int], bool] | None:
    """Pick one literal per branch as its select so that all selects exclude each other.
    Returns (selects, exactly_one) or None."""
    best = None
    for pick in itertools.product(*branches):
        if len(set(pick)) < len(pick):
            continue
        if all(oracle.exclusive(a, b) for a, b in itertools.combinations(pick, 2)):
            if oracle.one_always(list(pick)):
                return list(pick), True
            if best is None:
                best = (list(pick), False)
    return best


def use_buses(cn: CellNet, oracle: StateOracle, log=print) -> int:
    n = 0
    for c in cn.cells:
        name = "P" + c.type
        if name not in BUS:
            continue
        br = _branches(c.type, c.ins)
        if br is None:
            continue
        got = choose_selects(br, oracle)
        if got is None:
            continue
        sels, exact = got
        new = name + "X" if exact else name
        if ALL[new].area < ALL[c.type].area:
            log(f"  {c.type} -> {new} (selects {sels})")
            c.type = new
            n += 1
    return n


def _inverse_available(cn: CellNet) -> set[int]:
    return {s for c in cn.cells if c.type == "INV" for s in (c.ins[0], c.y)}


def use_latch_muxes(cn: CellNet, min_share: int = 3) -> int:
    loads: dict[int, int] = {}
    for c in cn.cells:
        for s in c.ins:
            loads[s] = loads.get(s, 0) + 1
    for bits in cn.outputs.values():
        for s in bits:
            loads[s] = loads.get(s, 0) + 2         # a port is never latch-only
    d_count: dict[int, int] = {}
    for d, _ in cn.dffs:
        d_count[d] = d_count.get(d, 0) + 1
    inv = _inverse_available(cn)
    groups: dict[int, list] = {}
    for c in cn.cells:
        if (c.type == "MUX2I" and d_count.get(c.y) == 1 and loads.get(c.y, 0) == 0
                and c.ins[0] in inv and c.ins[1] in inv):
            groups.setdefault(c.ins[2], []).append(c)
    n = 0
    for sel, cs in groups.items():
        if len(cs) >= min_share:
            for c in cs:
                c.type = "LMUX2I"
                n += 1
    return n


def total_transistors(cn: CellNet) -> int:
    return cn.area + mux_select_cost(cn) + latch_clock_cost(cn) + DFF_TRANSISTORS * len(cn.dffs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cells", type=Path, required=True, help="cell netlist (from pass_rewrite.py)")
    ap.add_argument("--inv", type=Path, default=None, help="register invariants of its expansion")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cn = load_cells(args.cells)
    before = total_transistors(cn)
    inv = load_invariants(args.inv) if args.inv else []
    oracle = StateOracle(expand(cn), inv)
    nb = use_buses(cn, oracle, log=print if args.verbose else (lambda *a: None))
    after_bus = total_transistors(cn)
    nl = use_latch_muxes(cn)
    after = total_transistors(cn)
    oracle.close()
    kinds: dict[str, int] = {}
    for c in cn.cells:
        if c.type.startswith("P") or c.type == "LMUX2I":
            kinds[c.type] = kinds.get(c.type, 0) + 1
    print(f"{args.cells}: {before} transistors ({len(inv)} invariants)")
    print(f"  buses: {nb} cells -> {after_bus}")
    print(f"  latch muxes: {nl} cells, gated clocks +{latch_clock_cost(cn)} -> {after}")
    print("  new cells:", dict(sorted(kinds.items(), key=lambda kv: -kv[1])))
    if args.out:
        save_cells(cn, args.out)
        save_circuit(expand(cn), args.out.with_name(args.out.stem + "_gates.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
