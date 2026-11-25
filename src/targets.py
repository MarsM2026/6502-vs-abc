"""Target circuits, defined by exhaustive truth tables.

A target is a pure function from an input vector (int, bit j = input j) to an
output vector (int, bit k = output k). We turn it into bit-parallel patterns:
pattern[j] has bit v set iff input j is 1 in vector v.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Target:
    name: str
    input_names: list[str]
    output_names: list[str]
    input_patterns: list[int]
    output_patterns: list[int]
    mask: int
    # Number of test vectors when only a subset of the 2^n input combinations
    # matters (don't-cares); None means exhaustive.
    vectors: int | None = None

    @property
    def n_inputs(self) -> int:
        return len(self.input_names)

    @property
    def n_outputs(self) -> int:
        return len(self.output_names)

    @property
    def n_vectors(self) -> int:
        return (1 << self.n_inputs) if self.vectors is None else self.vectors

    @property
    def max_score(self) -> int:
        return self.n_outputs * self.n_vectors


def build_target(name: str, input_names: list[str], output_names: list[str],
                 fn: Callable[[int], int]) -> Target:
    n_in, n_out = len(input_names), len(output_names)
    if n_in > 20:
        raise ValueError("exhaustive truth tables above 20 inputs are impractical; use SAT equivalence")
    n_vec = 1 << n_in
    in_pat = [0] * n_in
    out_pat = [0] * n_out
    for v in range(n_vec):
        bit = 1 << v
        for j in range(n_in):
            if (v >> j) & 1:
                in_pat[j] |= bit
        out = fn(v)
        for k in range(n_out):
            if (out >> k) & 1:
                out_pat[k] |= bit
    return Target(name, input_names, output_names, in_pat, out_pat, (1 << n_vec) - 1)


def adder(n: int) -> Target:
    """n-bit ripple adder: inputs a0..a(n-1), b0..b(n-1), cin; outputs s0..s(n-1), cout."""
    def fn(v: int) -> int:
        a = v & ((1 << n) - 1)
        b = (v >> n) & ((1 << n) - 1)
        cin = (v >> (2 * n)) & 1
        return a + b + cin  # bit n is carry-out
    ins = [f"a{i}" for i in range(n)] + [f"b{i}" for i in range(n)] + ["cin"]
    outs = [f"s{i}" for i in range(n)] + ["cout"]
    return build_target(f"adder{n}", ins, outs, fn)


ALU_OPS = ["ADC", "AND", "ORA", "EOR"]  # 6502 mnemonics for the four operations


def alu4() -> Target:
    """A 6502-flavoured 4-bit ALU slice.

    inputs : a0-3, b0-3, cin, op0, op1
    outputs: r0-3, c (carry, only for ADC), z (result == 0)
    op = 0 ADC (a+b+cin), 1 AND, 2 ORA, 3 EOR
    """
    def fn(v: int) -> int:
        a = v & 0xF
        b = (v >> 4) & 0xF
        cin = (v >> 8) & 1
        op = (v >> 9) & 3
        c = 0
        if op == 0:
            s = a + b + cin
            r, c = s & 0xF, s >> 4
        elif op == 1:
            r = a & b
        elif op == 2:
            r = a | b
        else:
            r = a ^ b
        z = int(r == 0)
        return r | (c << 4) | (z << 5)
    ins = [f"a{i}" for i in range(4)] + [f"b{i}" for i in range(4)] + ["cin", "op0", "op1"]
    outs = [f"r{i}" for i in range(4)] + ["c", "z"]
    return build_target("alu4", ins, outs, fn)


TARGETS: dict[str, Callable[[], Target]] = {
    "adder1": lambda: adder(1),
    "adder2": lambda: adder(2),
    "adder3": lambda: adder(3),
    "adder4": lambda: adder(4),
    "alu4": alu4,
}
