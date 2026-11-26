"""NMOS cell netlists: a netlist whose nodes are cells of the library in nmos_map.py.

A mapped netlist (ABC onto build/nmos.lib, read back from Yosys JSON) has an *exact*
transistor count: the sum of its cell areas, plus 6 per flip-flop. cell_windows.py
optimises such a netlist directly, so ABC's restructuring cannot undo the gain.

  CellNet               cells (type, inputs, output), flip-flops, ports
  load_mapped(path)     read the Yosys JSON written by nmos_map.py
  map_netlist(...)      run nmos_map on a gate netlist and load the result
  expand(cn)            two-input gate Circuit with the same signals (for SAT and csim)
  CELLS                 name -> Cell(arity, area, fn), fn evaluates bit-parallel
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from netlist import Circuit, Gate, _topo_sort
from nmos_map import ALL_CELLS, DFF_TRANSISTORS, LIBRARIES, PIN_NAMES, PIN_RE

PINS = PIN_NAMES


@dataclass(frozen=True)
class Cell:
    name: str
    arity: int
    area: int
    expr: str          # pull-down function over A..P (may use !pin); the cell computes its negation
    fn: object         # fn(values: list[int], mask: int) -> int


def _make_cell(name: str, expr: str, area: int) -> Cell:
    pins = sorted(set(re.findall(PIN_RE, expr)))
    assert pins == list(PINS[:len(pins)]), name
    body = re.sub(PIN_RE, lambda m: f"v[{PINS.index(m.group())}]", expr).replace("!", "~")
    fn = eval(f"lambda v, m: ~({body}) & m")  # noqa: S307 (expressions are our own constants)
    return Cell(name, len(pins), area, expr, fn)


# ALL_CELLS: every cell any library variant can produce (for reading and simulating mapped
# netlists). CELLS: the static library, which cell_windows.py evolves with.
ALL: dict[str, Cell] = {n: _make_cell(n, e, a) for n, (e, a) in ALL_CELLS.items()}
CELLS: dict[str, Cell] = {n: ALL[n] for n in LIBRARIES["static"]}


@dataclass
class CellInst:
    type: str
    ins: list[int]
    y: int


@dataclass
class CellNet:
    inputs: dict[str, list[int]]
    outputs: dict[str, list[int]]
    cells: list[CellInst]             # topologically sorted
    dffs: list[tuple[int, int]]       # (d, q)
    n_signals: int
    names: dict[int, str] = field(default_factory=dict)

    @property
    def area(self) -> int:
        return sum(ALL[c.type].area for c in self.cells)

    @property
    def total(self) -> int:
        return self.area + DFF_TRANSISTORS * len(self.dffs)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for c in self.cells:
            counts[c.type] = counts.get(c.type, 0) + 1
        top = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        return (f"{len(self.cells)} cells, {self.area} transistors + {len(self.dffs)} flip-flops x "
                f"{DFF_TRANSISTORS} = {self.total} ({top})")


def topo_sort(cn: CellNet) -> None:
    """Sort cells so every cell comes after its drivers; ValueError on a loop."""
    driver = {c.y: c for c in cn.cells}
    ready = {0, 1} | {s for b in cn.inputs.values() for s in b} | {q for _, q in cn.dffs}
    state: dict[int, int] = {}
    order: list[CellInst] = []
    for root in cn.cells:
        if state.get(root.y) == 2:
            continue
        stack = [(root, False)]
        while stack:
            c, done = stack.pop()
            if done:
                state[c.y] = 2
                order.append(c)
                continue
            if state.get(c.y) == 2:
                continue
            if state.get(c.y) == 1:
                raise ValueError(f"combinational loop through signal {c.y}")
            state[c.y] = 1
            stack.append((c, True))
            for s in c.ins:
                if s in ready or state.get(s) == 2:
                    continue
                if s not in driver:
                    raise ValueError(f"signal {s} is used but never driven")
                if state.get(s) == 1:
                    raise ValueError(f"combinational loop through signal {s}")
                stack.append((driver[s], False))
    cn.cells = order


def load_mapped(path: str | Path, top: str = "cpu") -> CellNet:
    mod = json.loads(Path(path).read_text())["modules"][top]
    remap: dict[object, int] = {"0": 0, "1": 1, "x": 0, "z": 0}

    def sig(bit) -> int:
        if bit not in remap:
            remap[bit] = len(remap) - 2
        return remap[bit]

    inputs = {n: [sig(b) for b in p["bits"]] for n, p in mod["ports"].items() if p["direction"] == "input"}
    outputs = {n: [sig(b) for b in p["bits"]] for n, p in mod["ports"].items() if p["direction"] == "output"}
    cells, dffs, alias = [], [], {}
    for name, cell in mod["cells"].items():
        t, con = cell["type"], cell["connections"]
        if t == "$_DFF_P_":
            dffs.append((sig(con["D"][0]), sig(con["Q"][0])))
        elif t in ALL:
            cells.append(CellInst(t, [sig(con[p][0]) for p in PINS[:ALL[t].arity]], sig(con["Y"][0])))
        elif t == "$_BUF_":
            alias[sig(con["Y"][0])] = sig(con["A"][0])
        else:
            raise ValueError(f"unsupported cell {t} ({name})")

    def r(s: int) -> int:
        while s in alias:
            s = alias[s]
        return s
    for c in cells:
        c.ins = [r(s) for s in c.ins]
    cn = CellNet(inputs, {n: [r(s) for s in b] for n, b in outputs.items()}, cells,
                 [(r(d), q) for d, q in dffs], max(remap.values()) + 1)
    topo_sort(cn)
    return cn


def map_netlist(netlist: Path, script: str | None = None, oss_cad: Path = Path("C:/tools/oss-cad-suite"),
                lib: str = "static") -> CellNet:
    """Map a gate netlist (our JSON) onto an NMOS library variant with ABC and load the cells."""
    import nmos_map
    from netlist import load_circuit, save_verilog
    lib_file = nmos_map.write_liberty(variant=lib)
    v = nmos_map.ROOT / "build" / "nmos_in.v"
    save_verilog(load_circuit(netlist), v)
    nmos_map.run_yosys(f"read_verilog {v.relative_to(nmos_map.ROOT).as_posix()}\nproc\nopt -nodffe -nosdff\ntechmap",
                       oss_cad, script=script, lib=lib_file)
    return load_mapped(nmos_map.MAPPED_JSON)


def _parse(expr: str):
    """Pull-down expression -> nested ('&'|'|', [children]) / pin index."""
    pos = 0

    def term():
        nonlocal pos
        if expr[pos] == "!":
            pos += 1
            return ("!", [term()])
        if expr[pos] == "(":
            pos += 1
            t = alt()
            pos += 1      # ')'
            return t
        pos += 1
        return PINS.index(expr[pos - 1])

    def seq(op, sub):
        nonlocal pos
        items = [sub()]
        while pos < len(expr) and expr[pos] == op:
            pos += 1
            items.append(sub())
        return items[0] if len(items) == 1 else (op, items)

    def conj():
        return seq("&", term)

    def alt():
        return seq("|", conj)

    return alt()


_TREES = {n: _parse(c.expr) for n, c in ALL.items()}


def expand(cn: CellNet) -> Circuit:
    """Two-input gate netlist computing the same signals (cell outputs keep their ids)."""
    gates: list[Gate] = []
    nxt = [cn.n_signals]

    def new() -> int:
        nxt[0] += 1
        return nxt[0] - 1

    def build(t, ins) -> int:
        if isinstance(t, int):
            return ins[t]
        op, kids = t
        if op == "!":
            x = build(kids[0], ins)
            y = new()
            gates.append(Gate("NOT", x, x, y))
            return y
        acc = build(kids[0], ins)
        for k in kids[1:]:
            y = new()
            gates.append(Gate("AND" if op == "&" else "OR", acc, build(k, ins), y))
            acc = y
        return acc

    for c in cn.cells:
        f = build(_TREES[c.type], c.ins)
        gates.append(Gate("NOT", f, f, c.y))
    return Circuit(dict(cn.inputs), {k: list(v) for k, v in cn.outputs.items()},
                   _topo_sort(gates, cn.inputs, cn.dffs), list(cn.dffs), nxt[0], dict(cn.names))


def save_cells(cn: CellNet, path: str | Path) -> None:
    Path(path).write_text(json.dumps({
        "inputs": cn.inputs, "outputs": cn.outputs, "n_signals": cn.n_signals,
        "cells": [[c.type, c.ins, c.y] for c in cn.cells], "dffs": cn.dffs}))


def load_cells(path: str | Path) -> CellNet:
    d = json.loads(Path(path).read_text())
    cn = CellNet(d["inputs"], d["outputs"], [CellInst(t, ins, y) for t, ins, y in d["cells"]],
                 [tuple(x) for x in d["dffs"]], d["n_signals"])
    topo_sort(cn)
    return cn
