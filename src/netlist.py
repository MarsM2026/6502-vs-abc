"""Load a Yosys JSON netlist into a flat, sequential gate-level circuit.

The result uses exactly the CGP gate set from ``cgp.py`` (AND OR XOR NAND NOR
XNOR NOT) plus plain positive-edge D flip-flops, so combinational blocks between
the flip-flops can later be cut out and optimised with CGP.

Signals are small integers: 0 = constant 0, 1 = constant 1, the rest are wires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

GATE_OPS = ("AND", "OR", "XOR", "NAND", "NOR", "XNOR", "NOT")

_SIMPLE = {
    "$_AND_": "AND", "$_OR_": "OR", "$_XOR_": "XOR",
    "$_NAND_": "NAND", "$_NOR_": "NOR", "$_XNOR_": "XNOR", "$_NOT_": "NOT",
}


@dataclass
class Gate:
    op: str
    a: int
    b: int  # == a for NOT
    y: int


@dataclass
class Circuit:
    inputs: dict[str, list[int]]
    outputs: dict[str, list[int]]
    gates: list[Gate]                 # topologically sorted
    dffs: list[tuple[int, int]]       # (d, q)
    n_signals: int
    names: dict[int, str] = field(default_factory=dict)

    @property
    def gate_count(self) -> int:
        return len(self.gates)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for g in self.gates:
            counts[g.op] = counts.get(g.op, 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        return f"{self.gate_count} gates ({parts}), {len(self.dffs)} flip-flops, {self.n_signals} signals"


def save_circuit(c: Circuit, path: str | Path) -> None:
    Path(path).write_text(json.dumps({
        "inputs": c.inputs, "outputs": c.outputs, "n_signals": c.n_signals,
        "gates": [[g.op, g.a, g.b, g.y] for g in c.gates], "dffs": c.dffs,
        "names": {str(k): v for k, v in c.names.items()},
    }))


_VEXPR = {"AND": "{a} & {b}", "OR": "{a} | {b}", "XOR": "{a} ^ {b}", "NAND": "~({a} & {b})",
          "NOR": "~({a} | {b})", "XNOR": "~({a} ^ {b})", "NOT": "~{a}"}


def save_verilog(c: Circuit, path: str | Path, top: str = "cpu", clk: str = "clk") -> None:
    """Structural Verilog of the circuit, so Yosys/ABC can re-optimise our netlists.
    Every flip-flop is clocked on the rising edge of input `clk`. If the circuit has no
    such input (the real-6502 conversion: its registers advance once per settling step),
    an extra port `clk` is added; it reads back as an unused input."""
    def ref(s: int) -> str:
        return {0: "1'b0", 1: "1'b1"}.get(s, f"n{s}")

    extra_clk = clk not in c.inputs
    ports = list(c.inputs) + list(c.outputs) + ([clk] if extra_clk else [])
    L = [f"module {top}(" + ", ".join(ports) + ");"]
    if extra_clk:
        L.append(f"  input {clk};")
    for kind, ports in (("input", c.inputs), ("output", c.outputs)):
        for name, bits in ports.items():
            L.append(f"  {kind} [{len(bits) - 1}:0] {name};" if len(bits) > 1 else f"  {kind} {name};")
    for name, bits in c.inputs.items():
        for i, s in enumerate(bits):
            L.append(f"  wire n{s} = {name}[{i}];" if len(bits) > 1 else f"  wire n{s} = {name};")
    for _, q in c.dffs:
        L.append(f"  reg n{q};")
    for g in c.gates:
        L.append(f"  wire n{g.y} = " + _VEXPR[g.op].format(a=ref(g.a), b=ref(g.b)) + ";")
    clk_sig = clk if extra_clk else ref(c.inputs[clk][0])
    L.append(f"  always @(posedge {clk_sig}) begin")
    for d, q in c.dffs:
        L.append(f"    n{q} <= {ref(d)};")
    L.append("  end")
    for name, bits in c.outputs.items():
        for i, s in enumerate(bits):
            L.append(f"  assign {name}[{i}] = {ref(s)};" if len(bits) > 1 else f"  assign {name} = {ref(s)};")
    L.append("endmodule")
    Path(path).write_text("\n".join(L) + "\n")


def load_circuit(path: str | Path) -> Circuit:
    d = json.loads(Path(path).read_text())
    if "modules" in d:
        return load_yosys_json(path)
    gates = [Gate(*g) for g in d["gates"]]
    dffs = [tuple(x) for x in d["dffs"]]
    return Circuit(d["inputs"], d["outputs"], _topo_sort(gates, d["inputs"], dffs), dffs,
                   d["n_signals"], {int(k): v for k, v in d.get("names", {}).items()})


def resort(c: Circuit) -> None:
    c.gates = _topo_sort(c.gates, c.inputs, c.dffs)


def substitute(c: Circuit, mapping: dict[int, int]) -> None:
    """Replace every *use* of signal k by mapping[k] (gate inputs, flip-flop D, outputs)."""
    if not mapping:
        return

    def r(s: int) -> int:
        while s in mapping:
            s = mapping[s]
        return s

    for g in c.gates:
        g.a, g.b = r(g.a), r(g.b)
    c.dffs = [(r(d), q) for d, q in c.dffs]
    c.outputs = {n: [r(s) for s in bits] for n, bits in c.outputs.items()}


def sweep(c: Circuit) -> int:
    """Remove gates whose output is never used. Returns number removed."""
    before = len(c.gates)
    while True:
        used = {s for bits in c.outputs.values() for s in bits} | {d for d, _ in c.dffs}
        for g in c.gates:
            used.add(g.a)
            used.add(g.b)
        kept = [g for g in c.gates if g.y in used]
        if len(kept) == len(c.gates):
            break
        c.gates = kept
    return before - len(c.gates)


def cleanup(c: Circuit) -> None:
    """Cheap structural simplification (the kind ABC would do for free):
    constant propagation, double negation, trivial identities (a&a, a|0, ...),
    and merging structurally identical gates. Repeats until nothing changes."""
    while True:
        mapping: dict[int, int] = {}
        seen: dict[tuple, int] = {}
        kept: list[Gate] = []
        driver_not: dict[int, int] = {}  # y -> a for NOT gates

        def r(s: int) -> int:
            while s in mapping:
                s = mapping[s]
            return s

        for g in c.gates:
            a, b = r(g.a), r(g.b)
            op = g.op
            if op == "NOT":
                b = a
            res = None
            if op == "NOT":
                if a < 2:
                    res = 1 - a
                elif a in driver_not:
                    res = driver_not[a]
            else:
                lo, hi = min(a, b), max(a, b)
                inv = op in ("NAND", "NOR", "XNOR")
                base = {"NAND": "AND", "NOR": "OR", "XNOR": "XOR"}.get(op, op)
                val = None  # simplified value of the *base* op: int signal or ("not", s)
                if base == "AND":
                    if lo == 0:
                        val = 0
                    elif lo == 1:
                        val = hi
                    elif lo == hi:
                        val = lo
                elif base == "OR":
                    if lo == 1:
                        val = 1
                    elif lo == 0:
                        val = hi
                    elif lo == hi:
                        val = lo
                elif base == "XOR":
                    if lo == hi:
                        val = 0
                    elif lo == 0:
                        val = hi
                if val is not None:
                    if not inv:
                        res = val
                    elif val < 2:
                        res = 1 - val
                    elif val in driver_not:
                        res = driver_not[val]
                    else:  # becomes a NOT of val
                        op, a, b = "NOT", val, val
                if res is None and op != "NOT":
                    a, b = lo, hi
            if res is not None:
                mapping[g.y] = res
                continue
            key = (op, a, b)
            if key in seen:
                mapping[g.y] = seen[key]
                continue
            seen[key] = g.y
            g.op, g.a, g.b = op, a, b
            if op == "NOT":
                driver_not[g.y] = a
            kept.append(g)
        c.gates = kept
        substitute(c, mapping)
        removed = sweep(c)
        if not mapping and not removed:
            return


def load_yosys_json(path: str | Path, top: str = "cpu") -> Circuit:
    mod = json.loads(Path(path).read_text())["modules"][top]

    # Map Yosys bit ids / constants onto compact ints.
    remap: dict[object, int] = {"0": 0, "1": 1, "x": 0, "z": 0}
    next_id = [2]

    def sig(bit) -> int:
        if bit not in remap:
            remap[bit] = next_id[0]
            next_id[0] += 1
        return remap[bit]

    def new_sig() -> int:
        next_id[0] += 1
        return next_id[0] - 1

    inputs = {n: [sig(b) for b in p["bits"]] for n, p in mod["ports"].items() if p["direction"] == "input"}
    outputs = {n: [sig(b) for b in p["bits"]] for n, p in mod["ports"].items() if p["direction"] == "output"}
    names: dict[int, str] = {}
    for n, info in mod.get("netnames", {}).items():
        if n.startswith("$"):
            continue
        for i, b in enumerate(info["bits"]):
            s = sig(b)
            if s > 1 and s not in names:
                names[s] = f"{n}[{i}]" if len(info["bits"]) > 1 else n

    gates: list[Gate] = []
    dffs: list[tuple[int, int]] = []
    aliases: dict[int, int] = {}
    not_cache: dict[int, int] = {}

    def inv(s: int) -> int:
        if s not in not_cache:
            y = new_sig()
            gates.append(Gate("NOT", s, s, y))
            not_cache[s] = y
        return not_cache[s]

    for cname, cell in mod["cells"].items():
        t, c = cell["type"], cell["connections"]
        one = lambda pin: sig(c[pin][0])  # noqa: E731
        if t in _SIMPLE:
            op = _SIMPLE[t]
            a = one("A")
            b = a if op == "NOT" else one("B")
            gates.append(Gate(op, a, b, one("Y")))
        elif t == "$_ANDNOT_":       # A & ~B
            gates.append(Gate("AND", one("A"), inv(one("B")), one("Y")))
        elif t == "$_ORNOT_":        # A | ~B
            gates.append(Gate("OR", one("A"), inv(one("B")), one("Y")))
        elif t == "$_MUX_":          # S ? B : A
            s, a, b, y = one("S"), one("A"), one("B"), one("Y")
            t1, t0 = new_sig(), new_sig()
            gates.append(Gate("AND", s, b, t1))
            gates.append(Gate("AND", inv(s), a, t0))
            gates.append(Gate("OR", t1, t0, y))
        elif t == "$_BUF_":
            aliases[one("Y")] = one("A")
        elif t == "$_DFF_P_":
            dffs.append((one("D"), one("Q")))
        else:
            raise ValueError(f"unsupported cell type {t} ({cname}); check the Yosys script")

    def resolve(s: int) -> int:
        while s in aliases:
            s = aliases[s]
        return s

    for g in gates:
        g.a, g.b = resolve(g.a), resolve(g.b)
    dffs = [(resolve(d), q) for d, q in dffs]
    outputs = {n: [resolve(s) for s in bits] for n, bits in outputs.items()}

    return Circuit(inputs, outputs, _topo_sort(gates, inputs, dffs), dffs, next_id[0], names)


def _topo_sort(gates: list[Gate], inputs: dict[str, list[int]], dffs: list[tuple[int, int]]) -> list[Gate]:
    driver = {}
    for g in gates:
        if g.y in driver:
            raise ValueError(f"signal {g.y} has multiple drivers")
        driver[g.y] = g
    ready = {0, 1} | {s for bits in inputs.values() for s in bits} | {q for _, q in dffs}
    order: list[Gate] = []
    state: dict[int, int] = {}  # 1 = visiting, 2 = done

    for root in gates:
        if state.get(root.y) == 2:
            continue
        stack = [(root, False)]
        while stack:
            g, expanded = stack.pop()
            if expanded:
                state[g.y] = 2
                order.append(g)
                continue
            if state.get(g.y) == 2:
                continue
            if state.get(g.y) == 1:
                raise ValueError(f"combinational loop through signal {g.y}")
            state[g.y] = 1
            stack.append((g, True))
            for s in (g.a, g.b):
                if s in ready or state.get(s) == 2:
                    continue
                if s not in driver:
                    raise ValueError(f"signal {s} is used but never driven")
                if state.get(s) == 1:
                    raise ValueError(f"combinational loop through signal {s}")
                stack.append((driver[s], False))
    return order
