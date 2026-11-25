"""Convert the real NMOS 6502 transistor netlist (visual6502) into a gate-level netlist.

Semantics we reproduce are those of perfect6502's switch-level simulator: nodes joined by
conducting transistors form a *group*, and the whole group takes one value, by priority

    connected to GND  ->  0
    connected to VCC  ->  1
    a pin forced low  ->  0
    a pull-up (static pull-up or pin forced high)  ->  1
    some node in the group was high (stored charge)  ->  1
    otherwise  ->  0

Construction:
  * Series/parallel pull-down networks (through internal nodes that drive no gate) become
    one boolean "conducts to GND" expression per node.
  * Remaining transistors between two signal nodes are *pass* switches. Nodes joined by
    pass switches form a channel-connected component (CCC); for each node we build
    "reachable within the CCC through conducting switches" logic for every priority class.
  * Stored charge and combinational feedback loops use the node's value from the previous
    *settling step*: every such node gets a register prev[n] (D = new value, Q = old value).
    The simulator repeats steps until no register changes, once after every input change.
So one settling step is a pure synchronous circuit and can be optimised like any other
netlist; the settle loop lives in the harness (src/csim_real.py).

Data: vendor/perfect6502/netlist_6502.h (visual6502, CC BY-NC-SA 3.0) and
vendor/visual6502/nodenames.js (MIT). Usage:  python src/real6502.py
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

from netlist import Circuit, Gate, cleanup, resort, save_circuit
from nmos import GND, VCC, Netlist, load

ROOT = Path(__file__).resolve().parent.parent
sys.setrecursionlimit(20000)

INPUT_PINS = ["res", "rdy", "so", "irq", "nmi", "clk0"] + [f"db{i}" for i in range(8)]
OUTPUT_PINS = [f"ab{i}" for i in range(16)] + [f"db{i}" for i in range(8)] + ["rw", "sync"]
# register bits, exported as extra outputs for debugging (from perfect6502's readA() etc.)
DEBUG = {"a": "a", "x": "x", "y": "y", "s": "s", "pcl": "pcl", "pch": "pch", "notir": "notir", "p": "p"}


class Builder:
    """Two-input gate netlist with constant folding and structural hashing."""

    def __init__(self, first_signal: int):
        self.gates: list[Gate] = []
        self.next = first_signal
        self.cache: dict[tuple, int] = {}

    def new(self) -> int:
        self.next += 1
        return self.next - 1

    def g(self, op: str, a: int, b: int) -> int:
        if op == "NOT":
            if a in (0, 1):
                return 1 - a
            key = ("NOT", a, a)
        else:
            if a > b:
                a, b = b, a
            if op == "AND":
                if a == 0: return 0
                if a == 1: return b
                if a == b: return a
            if op == "OR":
                if a == 1: return 1
                if a == 0: return b
                if a == b: return a
            key = (op, a, b)
        if key not in self.cache:
            y = self.new()
            self.gates.append(Gate(op, key[1], key[2], y))
            self.cache[key] = y
        return self.cache[key]

    def AND(self, *xs: int) -> int:
        r = 1
        for x in xs:
            r = self.g("AND", r, x)
        return r

    def OR(self, *xs: int) -> int:
        r = 0
        for x in xs:
            r = self.g("OR", r, x)
        return r

    def NOT(self, x: int) -> int:
        return self.g("NOT", x, x)


def node_ids(n: Netlist) -> dict[str, int]:
    return {v: k for k, v in n.names.items()} | {"sync": n_lookup(n, ("sync", "sync_"))}


def n_lookup(n: Netlist, cands) -> int:
    inv = {v: k for k, v in n.names.items()}
    for c in cands:
        if c in inv:
            return inv[c]
    raise KeyError(cands)


def convert(n: Netlist, log=print) -> tuple[Circuit, dict]:
    ids = node_ids(n)
    pins_in = {p: ids[p] for p in INPUT_PINS}
    pin_nodes = set(pins_in.values())
    gate_of = collections.defaultdict(list)          # node -> transistors it gates
    chan = collections.defaultdict(list)             # node -> transistors whose channel touches it
    for i, (g, a, b) in enumerate(n.trans):
        gate_of[g].append(i)
        chan[a].append(i)
        chan[b].append(i)

    # ---- internal (series-stack) nodes ------------------------------------------------
    internal = {x for x in chan if x not in (GND, VCC) and n.pullup[x] != 1
                and not gate_of.get(x) and x not in pin_nodes}

    def other(i: int, x: int) -> int:
        _, a, b = n.trans[i]
        return b if a == x else a

    # A node is a real "signal" node if it is not internal and not power.
    signal = {x for x in chan if x not in internal and x not in (GND, VCC)} | set(gate_of) - {GND, VCC}

    # Internal nodes must belong to exactly one top node's pull-down network; otherwise
    # they could join two signal nodes (sneak path), so treat them as signal nodes.
    while True:
        owner: dict[int, set[int]] = collections.defaultdict(set)

        def walk(x: int, top: int, seen: set):
            for i in chan[x]:
                y = other(i, x)
                if y in internal and y not in seen:
                    owner[y].add(top)
                    seen.add(y)
                    walk(y, top, seen)
        for s in signal:
            walk(s, s, set())
        shared = {y for y, tops in owner.items() if len(tops) > 1}
        dangling = {y for y in internal if not any(other(i, y) == GND or other(i, y) in internal for i in chan[y])
                    and y in owner}
        promote = shared | {y for y in internal if y not in owner}
        if not promote:
            break
        internal -= promote
        signal |= promote
    log(f"signal nodes {len(signal)}, internal series nodes {len(internal)}")

    # ---- classify transistors ---------------------------------------------------------
    pass_edges = collections.defaultdict(list)   # node -> [(other, gate)]
    vcc_gates = collections.defaultdict(list)    # node -> gates of transistors to VCC
    n_pass = 0
    for i, (g, a, b) in enumerate(n.trans):
        if a in signal and b in signal:
            pass_edges[a].append((b, g))
            pass_edges[b].append((a, g))
            n_pass += 1
        elif VCC in (a, b):
            x = b if a == VCC else a
            if x in internal:
                raise ValueError(f"transistor {i}: VCC through internal node not supported")
            if x != GND:
                vcc_gates[x].append(g)

    # ---- CCCs over pass edges -----------------------------------------------------------
    unit_of: dict[int, int] = {}
    units: list[list[int]] = []
    for s in sorted(signal):
        if s in unit_of:
            continue
        comp, stack = [], [s]
        unit_of[s] = len(units)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y, _ in pass_edges[x]:
                if y not in unit_of:
                    unit_of[y] = len(units)
                    stack.append(y)
        units.append(sorted(comp))
    sizes = collections.Counter(len(u) for u in units)
    log(f"pass transistors {n_pass}, CCCs {len(units)} (sizes {dict(sorted(sizes.items()))})")

    # ---- build gates ----------------------------------------------------------------
    names_in: dict[str, list[int]] = {}
    sig = 2
    pin_sig = {}
    for p in INPUT_PINS:
        pin_sig[p] = sig
        names_in[p] = [sig]
        sig += 1
    B = Builder(sig + len(signal) + 10)
    prev_q = {x: B.new() for x in signal}         # Q of prev[x]; unused ones are swept later
    val: dict[int, int] = {}
    state = {}                                    # unit -> "busy" / "done"
    used_prev: set[int] = set()

    def value_of(x: int) -> int:
        """Signal for node x's new value; previous-step value if we are inside a loop."""
        if x == GND:
            return 0
        if x == VCC:
            return 1
        u = unit_of[x]
        st = state.get(u)
        if st == "done":
            return val[x]
        if st == "busy":
            used_prev.add(x)
            return prev_q[x]
        build_unit(u)
        return val[x]

    def gnd_expr(x: int, seen: frozenset) -> int:
        terms = []
        for i in chan[x]:
            y = other(i, x)
            if y == GND:
                terms.append(value_of(n.trans[i][0]))
            elif y in internal and y not in seen:
                sub = gnd_expr(y, seen | {y})
                if sub != 0:
                    terms.append(B.AND(value_of(n.trans[i][0]), sub))
        return B.OR(*terms)

    pin_name = {v: k for k, v in pins_in.items()}

    def build_unit(u: int) -> None:
        state[u] = "busy"
        nodes = units[u]
        A, Bv, C, D, E = {}, {}, {}, {}, {}
        for x in nodes:
            A[x] = gnd_expr(x, frozenset({x}))
            Bv[x] = B.OR(*[value_of(g) for g in vcc_gates[x]])
            if x in pin_name:
                p = pin_sig[pin_name[x]]
                C[x], D[x] = B.NOT(p), p
            else:
                C[x], D[x] = 0, (1 if n.pullup[x] == 1 else 0)
            E[x] = prev_q[x]
        if len(nodes) > 1:
            # diameter of the CCC graph bounds the number of propagation rounds
            def ecc(s):
                dist, frontier = {s: 0}, [s]
                while frontier:
                    nxt = []
                    for a in frontier:
                        for b, _ in pass_edges[a]:
                            if b not in dist:
                                dist[b] = dist[a] + 1
                                nxt.append(b)
                    frontier = nxt
                return max(dist.values())
            rounds = max(ecc(s) for s in nodes)
            edges = {x: [(y, value_of(g)) for y, g in pass_edges[x]] for x in nodes}
            for P in (A, Bv, C, D, E):
                cur = dict(P)
                for _ in range(rounds):
                    cur = {x: B.OR(cur[x], *[B.AND(on, cur[y]) for y, on in edges[x]]) for x in nodes}
                P.update(cur)
        for x in nodes:
            val[x] = B.AND(B.NOT(A[x]), B.OR(Bv[x], B.AND(B.NOT(C[x]), B.OR(D[x], E[x]))))
        state[u] = "done"

    outs: dict[str, list[int]] = {}
    for p in OUTPUT_PINS:             # data pins are inputs too: outputs are called dbo0..7
        outs[p.replace("db", "dbo")] = [value_of(ids[p])]
    for k, base in DEBUG.items():
        bits = [ids.get(f"{base}{i}") for i in range(8)]
        if all(b is not None for b in bits):
            outs["dbg_" + k] = [value_of(b) for b in bits]
    for s in sorted(signal):                      # build everything, even unobserved nodes
        value_of(s)

    dffs = [(val[x], prev_q[x]) for x in sorted(signal)]
    c = Circuit({p: names_in[p] for p in INPUT_PINS}, outs, list(B.gates), dffs, B.next)
    info = {"signal_nodes": len(signal), "internal_nodes": len(internal), "pass": n_pass,
            "ccc_sizes": dict(sorted(sizes.items()))}
    return c, info


def prune_registers(c: Circuit) -> None:
    """Drop prev registers whose Q is never read (cleanup() keeps all dffs)."""
    while True:
        read = {s for g in c.gates for s in (g.a, g.b)} | {s for b in c.outputs.values() for s in b}
        read |= {d for d, q in c.dffs if q in read}
        keep = [(d, q) for d, q in c.dffs if q in read]
        if len(keep) == len(c.dffs):
            return
        c.dffs = keep
        from netlist import sweep
        sweep(c)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "real6502.json")
    args = ap.parse_args(argv)
    n = load()
    c, info = convert(n)
    print("converted:", info)
    resort(c)
    print("raw:      ", c.summary())
    prune_registers(c)
    cleanup(c)
    prune_registers(c)
    print("cleaned:  ", c.summary())
    save_circuit(c, args.out)
    print("saved", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
