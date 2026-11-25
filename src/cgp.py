"""Cartesian Genetic Programming (CGP) for real gate-level combinational circuits.

Unlike ``evolve.py`` (a pedagogical toy), every genome here *is* a netlist of
two-input logic gates, and fitness comes from simulating that netlist exhaustively.

Representation (classic single-row CGP, levels-back = unlimited):
  * ``n_inputs`` primary inputs, addressed 0 .. n_inputs-1
  * ``n_nodes`` gates; gate i has (func, in1, in2) and is addressed n_inputs+i.
    A gate may only read primary inputs or *earlier* gates, so the circuit is
    always feed-forward (no combinational loops).
  * one address per primary output.
Gates not reachable from any output are "inactive": they cost nothing but
carry hidden genetic material (neutral drift), which is what makes CGP work.

Simulation is bit-parallel: each signal is one Python int whose bit v is the
signal's value for input vector v. With exhaustive testing a 9-input circuit
is simulated for all 512 vectors with one integer op per gate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# name, arity (NOT ignores its second input)
GATES: list[tuple[str, int]] = [
    ("AND", 2),
    ("OR", 2),
    ("XOR", 2),
    ("NAND", 2),
    ("NOR", 2),
    ("XNOR", 2),
    ("NOT", 1),
]
GATE_INDEX = {name: i for i, (name, _) in enumerate(GATES)}


def apply_gate(func: int, a: int, b: int, mask: int) -> int:
    if func == 0:
        return a & b
    if func == 1:
        return a | b
    if func == 2:
        return a ^ b
    if func == 3:
        return ~(a & b) & mask
    if func == 4:
        return ~(a | b) & mask
    if func == 5:
        return ~(a ^ b) & mask
    return ~a & mask  # NOT


@dataclass
class Genome:
    n_inputs: int
    funcs: list[int]
    in1: list[int]
    in2: list[int]
    outputs: list[int]
    _active: list[int] | None = field(default=None, repr=False, compare=False)

    @property
    def n_nodes(self) -> int:
        return len(self.funcs)

    def copy(self) -> "Genome":
        return Genome(self.n_inputs, self.funcs[:], self.in1[:], self.in2[:], self.outputs[:])

    def active_nodes(self) -> list[int]:
        """Indices (0-based into the gate list) of gates that drive an output, ascending."""
        if self._active is None:
            n_in = self.n_inputs
            seen = [False] * self.n_nodes
            stack = [o - n_in for o in self.outputs if o >= n_in]
            while stack:
                i = stack.pop()
                if seen[i]:
                    continue
                seen[i] = True
                srcs = [self.in1[i]]
                if GATES[self.funcs[i]][1] == 2:
                    srcs.append(self.in2[i])
                stack.extend(s - n_in for s in srcs if s >= n_in and not seen[s - n_in])
            self._active = [i for i in range(self.n_nodes) if seen[i]]
        return self._active

    def gate_count(self) -> int:
        return len(self.active_nodes())

    def phenotype_key(self) -> tuple:
        """Everything that affects behaviour; equal keys => identical circuit outputs."""
        nodes = tuple(
            (i, self.funcs[i], self.in1[i], self.in2[i] if GATES[self.funcs[i]][1] == 2 else -1)
            for i in self.active_nodes()
        )
        return nodes, tuple(self.outputs)

    def simulate(self, input_patterns: list[int], mask: int) -> list[int]:
        values: list[int] = list(input_patterns) + [0] * self.n_nodes
        n_in = self.n_inputs
        for i in self.active_nodes():
            values[n_in + i] = apply_gate(self.funcs[i], values[self.in1[i]], values[self.in2[i]], mask)
        return [values[o] for o in self.outputs]


def random_genome(n_inputs: int, n_outputs: int, n_nodes: int, rng: random.Random) -> Genome:
    funcs = [rng.randrange(len(GATES)) for _ in range(n_nodes)]
    in1 = [rng.randrange(n_inputs + i) for i in range(n_nodes)]
    in2 = [rng.randrange(n_inputs + i) for i in range(n_nodes)]
    outputs = [rng.randrange(n_inputs + n_nodes) for _ in range(n_outputs)]
    return Genome(n_inputs, funcs, in1, in2, outputs)


def mutate_single_active(parent: Genome, rng: random.Random) -> Genome:
    """Goldman & Punch 'single active' mutation: keep mutating random genes until
    one that affects the phenotype has changed. Inactive changes along the way are
    kept too (neutral drift)."""
    child = parent.copy()
    active = set(parent.active_nodes())
    n_in, n_nodes = child.n_inputs, child.n_nodes
    genes_per_node = 3
    total_genes = n_nodes * genes_per_node + len(child.outputs)
    while True:
        g = rng.randrange(total_genes)
        if g >= n_nodes * genes_per_node:
            k = g - n_nodes * genes_per_node
            new = rng.randrange(n_in + n_nodes)
            if new != child.outputs[k]:
                child.outputs[k] = new
                return child
            continue
        node, which = divmod(g, genes_per_node)
        if which == 0:
            new = rng.randrange(len(GATES))
            if new == child.funcs[node]:
                continue
            child.funcs[node] = new
        else:
            arr = child.in1 if which == 1 else child.in2
            new = rng.randrange(n_in + node)
            if new == arr[node]:
                continue
            arr[node] = new
        # A NOT gate ignores in2, so changing it is neutral even on an active gate.
        ignored = which == 2 and GATES[child.funcs[node]][1] == 1
        if node in active and not ignored:
            return child


def to_netlist(g: Genome, input_names: list[str], output_names: list[str]) -> str:
    def name(addr: int) -> str:
        return input_names[addr] if addr < g.n_inputs else f"g{addr - g.n_inputs}"

    lines = []
    for i in g.active_nodes():
        gate, arity = GATES[g.funcs[i]]
        args = name(g.in1[i]) if arity == 1 else f"{name(g.in1[i])}, {name(g.in2[i])}"
        lines.append(f"g{i} = {gate}({args})")
    for o, out_name in zip(g.outputs, output_names):
        lines.append(f"{out_name} = {name(o)}")
    return "\n".join(lines)


def to_verilog(g: Genome, input_names: list[str], output_names: list[str], module: str) -> str:
    def name(addr: int) -> str:
        return input_names[addr] if addr < g.n_inputs else f"g{addr - g.n_inputs}"

    ops = {"AND": "{a} & {b}", "OR": "{a} | {b}", "XOR": "{a} ^ {b}",
           "NAND": "~({a} & {b})", "NOR": "~({a} | {b})", "XNOR": "~({a} ^ {b})", "NOT": "~{a}"}
    ports = ", ".join([f"input {n}" for n in input_names] + [f"output {n}" for n in output_names])
    lines = [f"module {module}({ports});"]
    for i in g.active_nodes():
        gate, _ = GATES[g.funcs[i]]
        expr = ops[gate].format(a=name(g.in1[i]), b=name(g.in2[i]))
        lines.append(f"  wire g{i} = {expr};")
    for o, out_name in zip(g.outputs, output_names):
        lines.append(f"  assign {out_name} = {name(o)};")
    lines.append("endmodule")
    return "\n".join(lines)
