"""Hand-built, known-correct starting circuits for *seeded* evolution.

This is the scaled-down version of the real plan: start from a working design
and let evolution remove gates while the fitness function guarantees correctness.
"""

from __future__ import annotations

import random

from cgp import GATE_INDEX, Genome, random_genome


class _Builder:
    def __init__(self, n_inputs: int):
        self.n_inputs = n_inputs
        self.funcs: list[int] = []
        self.in1: list[int] = []
        self.in2: list[int] = []

    def gate(self, name: str, a: int, b: int | None = None) -> int:
        self.funcs.append(GATE_INDEX[name])
        self.in1.append(a)
        self.in2.append(a if b is None else b)
        return self.n_inputs + len(self.funcs) - 1

    def finish(self, outputs: list[int], n_nodes: int, rng: random.Random) -> Genome:
        used = len(self.funcs)
        if used > n_nodes:
            raise ValueError(f"seed needs {used} gates but only {n_nodes} nodes available")
        # Pad with random (inactive) gates so evolution has spare material to work with.
        pad = random_genome(self.n_inputs, 0, n_nodes, rng)
        funcs = self.funcs + pad.funcs[used:]
        in1 = self.in1 + pad.in1[used:]
        in2 = self.in2 + pad.in2[used:]
        return Genome(self.n_inputs, funcs, in1, in2, outputs)


def ripple_carry_adder(n: int, n_nodes: int, rng: random.Random) -> Genome:
    """Textbook ripple-carry adder: 5 gates per bit (2 XOR, 2 AND, 1 OR).
    Input layout matches targets.adder(n)."""
    b = _Builder(2 * n + 1)
    carry = 2 * n
    sums = []
    for i in range(n):
        a_i, b_i = i, n + i
        x = b.gate("XOR", a_i, b_i)
        sums.append(b.gate("XOR", x, carry))
        t1 = b.gate("AND", a_i, b_i)
        t2 = b.gate("AND", x, carry)
        carry = b.gate("OR", t1, t2)
    return b.finish(sums + [carry], n_nodes, rng)


def textbook_alu4(n_nodes: int, rng: random.Random) -> Genome:
    """Straightforward 4-bit ALU slice matching targets.alu4 (67 gates).

    Per bit: XOR/AND/OR of a,b; a full adder reusing XOR and AND; then a 4:1 mux
    built from three AND-OR 2:1 muxes (op0 picks ADC/AND vs ORA/EOR, op1 the pair).
    Carry is gated to ADC only; Z is NOR of the four result bits.
    Inputs: a0-3 = 0-3, b0-3 = 4-7, cin = 8, op0 = 9, op1 = 10.
    """
    b = _Builder(11)
    op0, op1 = 9, 10
    n_op0 = b.gate("NOT", op0)
    n_op1 = b.gate("NOT", op1)

    def mux(sel: int, n_sel: int, one: int, zero: int) -> int:
        return b.gate("OR", b.gate("AND", sel, one), b.gate("AND", n_sel, zero))

    carry = 8
    results = []
    for i in range(4):
        a_i, b_i = i, 4 + i
        x = b.gate("XOR", a_i, b_i)
        n = b.gate("AND", a_i, b_i)
        o = b.gate("OR", a_i, b_i)
        s = b.gate("XOR", x, carry)
        carry = b.gate("OR", n, b.gate("AND", x, carry))
        low = mux(op0, n_op0, n, s)    # op1=0: op0 ? AND : ADC
        high = mux(op0, n_op0, x, o)   # op1=1: op0 ? EOR : ORA
        results.append(mux(op1, n_op1, high, low))
    is_adc = b.gate("NOR", op0, op1)
    c = b.gate("AND", carry, is_adc)
    z = b.gate("NOR", b.gate("OR", results[0], results[1]), b.gate("OR", results[2], results[3]))
    return b.finish(results + [c, z], n_nodes, rng)


SEEDS = {
    "alu4": textbook_alu4,
    "adder1": lambda n_nodes, rng: ripple_carry_adder(1, n_nodes, rng),
    "adder2": lambda n_nodes, rng: ripple_carry_adder(2, n_nodes, rng),
    "adder3": lambda n_nodes, rng: ripple_carry_adder(3, n_nodes, rng),
    "adder4": lambda n_nodes, rng: ripple_carry_adder(4, n_nodes, rng),
}
