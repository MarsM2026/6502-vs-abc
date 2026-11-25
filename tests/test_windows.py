"""Tests for cleanup() and window optimisation. Run: python -m unittest discover -s tests"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netlist import Circuit, Gate, cleanup, resort  # noqa: E402
from dontcare import reachable_combos  # noqa: E402
from cgp import GATE_INDEX, Genome  # noqa: E402
from windows import optimise, splice  # noqa: E402
from invariants import candidates, prove, simulate_states  # noqa: E402

OPS = ["AND", "OR", "XOR", "NAND", "NOR", "XNOR", "NOT"]


def random_circuit(n_in, n_gates, n_out, rng, redundancy=True):
    """Random feed-forward circuit; signals 2.. are inputs, then gates.
    With redundancy, deliberately adds duplicate gates and double negations."""
    ins = list(range(2, 2 + n_in))
    sigs = list(ins)
    gates = []
    nxt = 2 + n_in
    for _ in range(n_gates):
        op = rng.choice(OPS)
        a = rng.choice(sigs)
        b = a if op == "NOT" else rng.choice(sigs)
        if redundancy and gates and rng.random() < 0.15:
            g0 = rng.choice(gates)
            op, a, b = g0.op, g0.a, g0.b   # duplicate
        if redundancy and rng.random() < 0.05:
            b = a = 0 if rng.random() < 0.5 else 1  # constant input
            op = "AND" if op == "NOT" else op
        gates.append(Gate(op, a, b, nxt))
        sigs.append(nxt)
        nxt += 1
    outs = rng.sample(sigs[n_in:], n_out)
    c = Circuit({"x": ins}, {"y": outs}, gates, [], nxt)
    resort(c)
    return c


def truth_table(c):
    ins = c.inputs["x"]
    n = len(ins)
    nv = 1 << n
    mask = (1 << nv) - 1
    val = {0: 0, 1: mask}
    for j, s in enumerate(ins):
        val[s] = sum(1 << v for v in range(nv) if (v >> j) & 1)
    f = {"AND": lambda a, b: a & b, "OR": lambda a, b: a | b, "XOR": lambda a, b: a ^ b,
         "NAND": lambda a, b: ~(a & b) & mask, "NOR": lambda a, b: ~(a | b) & mask,
         "XNOR": lambda a, b: ~(a ^ b) & mask, "NOT": lambda a, b: ~a & mask}
    for g in c.gates:
        val[g.y] = f[g.op](val[g.a], val[g.b])
    return [val[s] for s in c.outputs["y"]]


class CleanupTests(unittest.TestCase):
    def test_cleanup_preserves_function_and_never_grows(self):
        for seed in range(20):
            c = random_circuit(6, 60, 4, random.Random(seed))
            before_tt, before_n = truth_table(c), c.gate_count
            cleanup(c)
            self.assertEqual(truth_table(c), before_tt, f"seed {seed}")
            self.assertLessEqual(c.gate_count, before_n)

    def test_double_negation_and_duplicates_removed(self):
        # y0 = NOT(NOT(x)), y1 = AND(x, z), y2 = AND(z, x)  -> 1 gate (the AND)
        g = [Gate("NOT", 2, 2, 4), Gate("NOT", 4, 4, 5), Gate("AND", 2, 3, 6), Gate("AND", 3, 2, 7)]
        c = Circuit({"x": [2, 3]}, {"y": [5, 6, 7]}, g, [], 8)
        cleanup(c)
        self.assertEqual(c.gate_count, 1)
        self.assertEqual(c.outputs["y"][0], 2)


class WindowTests(unittest.TestCase):
    def test_window_optimisation_preserves_function(self):
        for seed in range(4):
            rng = random.Random(seed)
            c = random_circuit(7, 80, 5, rng)
            cleanup(c)
            before_tt, before_n = truth_table(c), c.gate_count
            optimise(c, passes=1, k=6, max_gates=12, gens=300, rng=rng, log=lambda *_: None)
            self.assertEqual(truth_table(c), before_tt, f"seed {seed}")
            self.assertLessEqual(c.gate_count, before_n)


class SpliceTests(unittest.TestCase):
    def test_splice_rejects_combinational_loop(self):
        # Non-convex window {g0, g2}: s3 = NOT x leaves the window, s4 = NOT s3 comes back in.
        gates = [Gate("NOT", 2, 2, 3), Gate("NOT", 3, 3, 4), Gate("AND", 4, 2, 5)]
        c = Circuit({"x": [2]}, {"y": [5]}, gates, [], 6)
        resort(c)
        members = {i for i, g in enumerate(c.gates) if g.y in (3, 5)}
        ins, outs = [2, 4], [3, 5]
        # Replacement wires output s3 to AND(x, s4); s4 depends on s3 -> loop.
        bad = Genome(2, [GATE_INDEX["AND"], GATE_INDEX["NOT"]], [0, 0], [1, 0], [2, 3])
        before = [(g.op, g.a, g.b, g.y) for g in c.gates]
        self.assertFalse(splice(c, members, ins, outs, bad))
        self.assertEqual([(g.op, g.a, g.b, g.y) for g in c.gates], before)
        self.assertEqual(c.outputs, {"y": [5]})


class DontCareTests(unittest.TestCase):
    def test_reachable_combos_excludes_impossible(self):
        # w0 = x, w1 = NOT x  -> only (w0,w1) = (1,0) and (0,1) are possible; plus free input z.
        g = [Gate("NOT", 2, 2, 4)]
        c = Circuit({"x": [2, 3]}, {"y": [4]}, g, [], 5)
        combos = reachable_combos(c, [2, 4, 3])   # bits: x, not-x, z
        self.assertEqual(combos, sorted([0b010, 0b001, 0b110, 0b101]))

    def test_all_reachable_returns_none(self):
        c = Circuit({"x": [2, 3]}, {"y": [4]}, [Gate("AND", 2, 3, 4)], [], 5)
        self.assertIsNone(reachable_combos(c, [2, 3]))

    def test_constant_inputs_are_respected(self):
        c = Circuit({"x": [2]}, {"y": [3]}, [Gate("AND", 2, 1, 3)], [], 4)
        self.assertEqual(reachable_combos(c, [2, 1]), [0b10, 0b11])

    def test_window_optimisation_with_dont_cares_preserves_function(self):
        for seed in range(4):
            rng = random.Random(100 + seed)
            c = random_circuit(7, 80, 5, rng)
            cleanup(c)
            before_tt, before_n = truth_table(c), c.gate_count
            optimise(c, passes=1, k=6, max_gates=12, gens=300, rng=rng, log=lambda *_: None,
                     dont_cares=True)
            self.assertEqual(truth_table(c), before_tt, f"seed {seed}")
            self.assertLessEqual(c.gate_count, before_n)


class InvariantTests(unittest.TestCase):
    """q0' = NOT q0 (toggles), q1' = q0 AND q1 AND x. From the all-zero state q1 stays 0."""

    def circuit(self):
        gates = [Gate("NOT", 4, 4, 6), Gate("AND", 4, 5, 7), Gate("AND", 7, 3, 8)]
        return Circuit({"clk": [2], "x": [3]}, {"y": [8]}, gates, [(6, 4), (8, 5)], 9)

    def test_finds_and_proves_invariant(self):
        c = self.circuit()
        trace, nbits = simulate_states(c, 16, 20, random.Random(0), reset_cycles=0)
        inv = prove(c, candidates(c, trace, nbits), log=lambda *_: None)
        self.assertIn(((5, False),), inv)            # q1 is always 0
        self.assertNotIn(((4, False),), inv)         # q0 toggles

    def test_rejects_non_inductive_candidate(self):
        c = self.circuit()
        inv = prove(c, [((4, False),), ((5, False),)], log=lambda *_: None)
        self.assertEqual(inv, [((5, False),)])

    def test_invariants_restrict_reachable_combos(self):
        c = self.circuit()
        self.assertIsNone(reachable_combos(c, [4, 5]))
        self.assertEqual(reachable_combos(c, [4, 5], invariants=[((5, False),)]), [0b00, 0b01])


class NmosCostTests(unittest.TestCase):
    def test_cost_rules(self):
        from nmos_cost import dag_cost
        self.assertEqual(dag_cost({2: ("NOT", 0, 0)}, [2]), 2)                     # INV
        self.assertEqual(dag_cost({2: ("NAND", 0, 1)}, [2]), 3)                    # NAND2
        self.assertEqual(dag_cost({2: ("AND", 0, 1)}, [2]), 5)                     # NAND2 + INV
        self.assertEqual(dag_cost({2: ("XOR", 0, 1)}, [2]), 7)
        # NOR(AND(a,b), c) = AOI21: 3 pull-down transistors + pull-up
        self.assertEqual(dag_cost({3: ("AND", 0, 1), 4: ("NOR", 3, 2)}, [4]), 4)
        # same, but the AND is also an output -> cannot be absorbed
        self.assertEqual(dag_cost({3: ("AND", 0, 1), 4: ("NOR", 3, 2)}, [4, 3]), 5 + 3)

    def test_window_optimisation_with_nmos_cost_preserves_function(self):
        from nmos_cost import circuit_cost, genome_cost
        for seed in range(3):
            rng = random.Random(200 + seed)
            c = random_circuit(7, 80, 5, rng)
            cleanup(c)
            before_tt, before_cost = truth_table(c), circuit_cost(c)
            optimise(c, passes=1, k=6, max_gates=12, gens=300, rng=rng, log=lambda *_: None,
                     cost=genome_cost)
            self.assertEqual(truth_table(c), before_tt, f"seed {seed}")
            self.assertLessEqual(circuit_cost(c), before_cost)


if __name__ == "__main__":
    unittest.main()
