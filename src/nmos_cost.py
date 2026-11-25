"""Fast NMOS transistor-count estimate of a two-input gate network (fitness for CGP).

An NMOS static gate computes NOT(f), f a series/parallel network of k transistors, and
costs k + 1 transistors (k pull-down + 1 depletion pull-up). So a non-inverting AND/OR
whose only reader is an AND/OR/NAND/NOR/NOT can be *absorbed* into the reader's pull-down
network for free (that is how AOI/OAI gates arise). Rules, per gate that has to exist as a
real node ("materialised": it has fan-out > 1, drives an output, or its reader cannot
absorb it):

  NAND, NOR, NOT : 1 + literals          (literals = inputs after absorbing AND/OR children)
  AND, OR        : 1 + literals + 2      (the inverting gate plus an inverter)
  XOR, XNOR      : 7                     (AOI21 + NOR2, or OAI21 + NAND2)

It ignores the practical limit of about 8-9 series/parallel transistors per gate and
assumes both polarities of window inputs are not available (an input is one literal).
It is an estimate; the reference measure is ABC's mapping in nmos_map.py.
"""

from __future__ import annotations

from cgp import GATES, Genome

_ABSORBING = {"AND", "OR", "NAND", "NOR", "NOT"}
_ABSORBABLE = {"AND", "OR"}


def dag_cost(ops: dict[int, tuple[str, int, int]], outputs: list[int]) -> int:
    """ops: node id -> (op, a, b) for every gate (b == a for NOT); ids not in ops are inputs."""
    fanout: dict[int, int] = {}
    for op, a, b in ops.values():
        for s in ((a,) if op == "NOT" else (a, b)):
            fanout[s] = fanout.get(s, 0) + 1
    for o in outputs:
        fanout[o] = fanout.get(o, 0) + 1
    out_set = set(outputs)
    readers: dict[int, str] = {}
    for op, a, b in ops.values():
        for s in ((a,) if op == "NOT" else (a, b)):
            readers[s] = op

    def absorbed(s: int) -> bool:
        return (s in ops and ops[s][0] in _ABSORBABLE and fanout.get(s, 0) == 1
                and s not in out_set and readers.get(s) in _ABSORBING)

    def literals(s: int) -> int:
        op, a, b = ops[s]
        total = 0
        for x in ((a,) if op == "NOT" else (a, b)):
            total += literals(x) if absorbed(x) else 1
        return max(total, 1)

    cost = 0
    for s, (op, a, b) in ops.items():
        if absorbed(s):
            continue
        if op in ("XOR", "XNOR"):
            cost += 7
        elif op in ("AND", "OR"):
            cost += 1 + literals(s) + 2
        else:
            cost += 1 + literals(s)
    return cost


def genome_cost(g: Genome) -> int:
    n_in = g.n_inputs
    ops = {}
    for i in g.active_nodes():
        name, arity = GATES[g.funcs[i]][0], GATES[g.funcs[i]][1]
        a = g.in1[i]
        b = a if arity == 1 else g.in2[i]
        ops[n_in + i] = (name, a, b)
    # inputs are ids < n_in; constants are not special inside a genome
    return dag_cost(ops, list(g.outputs)) if ops else 0


def circuit_cost(c) -> int:
    ops = {g.y: (g.op, g.a, g.b) for g in c.gates}
    outs = [s for bits in c.outputs.values() for s in bits] + [d for d, _ in c.dffs]
    return dag_cost(ops, outs)
