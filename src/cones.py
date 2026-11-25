"""Analyse the combinational structure of a gate-level netlist.

Sources: primary inputs and flip-flop outputs (Q). Sinks: flip-flop inputs (D)
and primary outputs. For every sink we compute its *cone*: the gates it
transitively depends on, and its *support*: the sources it depends on. The
support size decides how a block can be checked: up to ~20 inputs exhaustively
(as in stage 1), above that with SAT-based equivalence checking.

Usage: python src/cones.py
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from netlist import Circuit, load_yosys_json

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Cone:
    sink: str
    signal: int
    gates: frozenset[int]     # indices into circuit.gates
    support: frozenset[int]   # source signals


def source_names(c: Circuit) -> dict[int, str]:
    names = {}
    for n, bits in c.inputs.items():
        for i, s in enumerate(bits):
            names[s] = f"{n}[{i}]" if len(bits) > 1 else n
    for i, (_, q) in enumerate(c.dffs):
        names[q] = c.names.get(q, f"ff{i}")
    return names


def sinks(c: Circuit) -> list[tuple[str, int]]:
    out = []
    for i, (d, q) in enumerate(c.dffs):
        out.append((f"D:{c.names.get(q, f'ff{i}')}", d))
    for n, bits in c.outputs.items():
        for i, s in enumerate(bits):
            out.append((f"{n}[{i}]" if len(bits) > 1 else n, s))
    return out


def compute_cones(c: Circuit) -> list[Cone]:
    driver = {g.y: i for i, g in enumerate(c.gates)}
    sources = {0, 1} | {s for b in c.inputs.values() for s in b} | {q for _, q in c.dffs}
    # Gates are topologically sorted, so one forward pass gives every signal's cone.
    gate_cone: dict[int, frozenset[int]] = {}
    sig_support: dict[int, frozenset[int]] = {s: (frozenset() if s < 2 else frozenset([s])) for s in sources}
    sig_cone: dict[int, frozenset[int]] = {s: frozenset() for s in sources}
    for i, g in enumerate(c.gates):
        ins = {g.a, g.b}
        sig_cone[g.y] = frozenset([i]).union(*(sig_cone[s] for s in ins))
        sig_support[g.y] = frozenset().union(*(sig_support[s] for s in ins))
    del gate_cone, driver
    return [Cone(name, s, sig_cone[s], sig_support[s]) for name, s in sinks(c)]


def main() -> None:
    c = load_yosys_json(ROOT / "build" / "cpu_gates.json")
    print("netlist:", c.summary())
    cones = compute_cones(c)
    n_sources = sum(len(b) for b in c.inputs.values()) + len(c.dffs)
    print(f"sources: {n_sources} (inputs + flip-flop outputs), sinks: {len(cones)}")

    sizes = sorted(len(k.support) for k in cones)
    buckets = Counter()
    for s in sizes:
        b = "0" if s == 0 else "1-8" if s <= 8 else "9-16" if s <= 16 else "17-20" if s <= 20 \
            else "21-32" if s <= 32 else "33-64" if s <= 64 else ">64"
        buckets[b] += 1
    print("\nsupport size per sink (number of sources it depends on):")
    for b in ["0", "1-8", "9-16", "17-20", "21-32", "33-64", ">64"]:
        print(f"  {b:>6}: {buckets[b]:4d} {'#' * buckets[b]}")

    exhaustive = [k for k in cones if len(k.support) <= 20]
    gates_exh = frozenset().union(*(k.gates for k in exhaustive)) if exhaustive else frozenset()
    gates_rest = frozenset().union(*(k.gates for k in cones if len(k.support) > 20))
    print(f"\nsinks checkable exhaustively (<=20 inputs): {len(exhaustive)}/{len(cones)}")
    print(f"gates only in such cones: {len(gates_exh - gates_rest)} / {c.gate_count}")

    shared = Counter(g for k in cones for g in k.gates)
    print(f"gates used by exactly one sink: {sum(1 for v in shared.values() if v == 1)}; "
          f"by >10 sinks: {sum(1 for v in shared.values() if v > 10)}")

    print("\nlargest cones:")
    for k in sorted(cones, key=lambda k: -len(k.gates))[:12]:
        print(f"  {k.sink:<22} gates {len(k.gates):5d}  support {len(k.support):4d}")
    print("\nsmallest non-trivial cones:")
    for k in sorted((k for k in cones if k.gates), key=lambda k: len(k.gates))[:8]:
        print(f"  {k.sink:<22} gates {len(k.gates):5d}  support {len(k.support):4d}")


if __name__ == "__main__":
    main()
