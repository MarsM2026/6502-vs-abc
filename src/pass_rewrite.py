"""Replace static-gate multiplexers in a mapped NMOS cell netlist by pass-transistor muxes.

ABC's area mapper `amap` (the best one for our library) ignores cells whose function is not
monotone, such as a multiplexer, and `map -a` is far worse overall. So we map with `amap`
and rewrite afterwards: an AOI22 or OAI22 in which one input is the inverse of another (an
INV cell connects them) computes a multiplexer:

  AOI22(d1, s, d0, !s) = !((d1 & s) | (d0 & !s)) = !(s ? d1 : d0) = MUX2I(d1, d0, s)
  OAI22(d1, !s, d0, s) = !((d1 | !s) & (d0 | s)) = !(s ? d1 : d0) = MUX2I(d1, d0, s)

MUX2I costs 4 transistors against 5, and needs !s, which the INV already provides
(nmos_map.mux_select_cost charges an inverter only where no INV of the select exists).
The rewrite is exact, so it needs no SAT check; tests/test_cells.py verifies it anyway.

Usage (from the project root):
  python src/pass_rewrite.py --netlist build/cpu_seqdc_deep600_it3.json --lib wide \
      --script "+strash;dc2;&get,-n;&dch,-f;&put;amap,-F,8,-A,16" --out build/cpu_pass.json
writes the cell netlist (JSON) and its two-input gate expansion (<out>_gates.json, for csim).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cells import CellInst, CellNet, expand, map_netlist, save_cells
from netlist import save_circuit
from nmos_map import DFF_TRANSISTORS, mux_select_cost


def _inverses(cn: CellNet) -> dict[int, set[int]]:
    inv: dict[int, set[int]] = {}
    for c in cn.cells:
        if c.type == "INV":
            inv.setdefault(c.ins[0], set()).add(c.y)
            inv.setdefault(c.y, set()).add(c.ins[0])
    return inv


def _as_mux(c: CellInst, inv: dict[int, set[int]]) -> list[int] | None:
    """[d1, d0, s] if the AOI22/OAI22 cell c is !(s ? d1 : d0), else None."""
    if c.type not in ("AOI22", "OAI22"):
        return None
    x = c.ins
    for p in (0, 1):
        for q in (2, 3):
            if x[q] not in inv.get(x[p], ()):
                continue
            first, second = x[1 - p], x[5 - q]
            # AOI22: the select is the pin that ANDs with the 1-data; OAI22: the pin that ORs
            # with the 0-data (then the other pair passes when it is 1). Use x[p] as s.
            if c.type == "AOI22":
                return [first, second, x[p]]
            return [second, first, x[p]]
    return None


def use_pass_muxes(cn: CellNet) -> int:
    """Rewrite in place; returns the number of cells replaced."""
    inv = _inverses(cn)
    n = 0
    for c in cn.cells:
        m = _as_mux(c, inv)
        if m is not None:
            c.type, c.ins = "MUX2I", m
            n += 1
    return n


def total_transistors(cn: CellNet) -> int:
    return cn.area + mux_select_cost(cn) + DFF_TRANSISTORS * len(cn.dffs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, required=True, help="gate netlist (our JSON)")
    ap.add_argument("--lib", default="wide", help="library variant for the amap step")
    ap.add_argument("--script", default="+strash;dc2;&get,-n;&dch,-f;&put;amap,-F,8,-A,16")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    cn = map_netlist(args.netlist, args.script, lib=args.lib)
    before = total_transistors(cn)
    n = use_pass_muxes(cn)
    after = total_transistors(cn)
    print(f"{args.netlist} [lib {args.lib}]: {before} transistors, {n} AOI22/OAI22 -> MUX2I, "
          f"select inverters +{mux_select_cost(cn)}, total {after}")
    if args.out:
        save_cells(cn, args.out)
        save_circuit(expand(cn), args.out.with_name(args.out.stem + "_gates.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
