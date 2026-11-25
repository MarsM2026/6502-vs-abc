"""Tests for the gate-level CGP engine. Run from the project root:

    python -m unittest discover -s tests
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cgp import GATE_INDEX, Genome, apply_gate, mutate_single_active, random_genome, to_verilog  # noqa: E402
from circuit_evolution import evolve, fitness_key, score  # noqa: E402
from seeds import ripple_carry_adder, textbook_alu4  # noqa: E402
from targets import adder, alu4  # noqa: E402


class GateTests(unittest.TestCase):
    def test_truth_tables(self):
        a, b, mask = 0b0011, 0b0101, 0b1111  # bits 0..3 hold (a,b) = 11, 10, 01, 00
        expected = {"AND": 0b0001, "OR": 0b0111, "XOR": 0b0110,
                    "NAND": 0b1110, "NOR": 0b1000, "XNOR": 0b1001, "NOT": 0b1100}
        for name, want in expected.items():
            self.assertEqual(apply_gate(GATE_INDEX[name], a, b, mask), want, name)


class TargetTests(unittest.TestCase):
    def test_adder_truth_table_matches_arithmetic(self):
        t = adder(3)
        for v in range(t.n_vectors):
            a, b, cin = v & 7, (v >> 3) & 7, (v >> 6) & 1
            out = sum(((t.output_patterns[k] >> v) & 1) << k for k in range(t.n_outputs))
            self.assertEqual(out, a + b + cin)

    def test_alu_ops_and_zero_flag(self):
        t = alu4()

        def run(a, b, cin, op):
            v = a | (b << 4) | (cin << 8) | (op << 9)
            return [(p >> v) & 1 for p in t.output_patterns]

        r = run(0xF, 0x1, 0, 0)            # ADC 15+1 = 0 carry 1
        self.assertEqual(r, [0, 0, 0, 0, 1, 1])
        r = run(0b1100, 0b1010, 1, 1)      # AND -> 1000, no carry even with cin
        self.assertEqual(r, [0, 0, 0, 1, 0, 0])
        r = run(0b1100, 0b1100, 0, 3)      # EOR -> 0, Z set
        self.assertEqual(r, [0, 0, 0, 0, 0, 1])


class GenomeTests(unittest.TestCase):
    def test_random_genome_is_feed_forward(self):
        g = random_genome(5, 3, 50, random.Random(1))
        for i in range(g.n_nodes):
            self.assertLess(g.in1[i], g.n_inputs + i)
            self.assertLess(g.in2[i], g.n_inputs + i)

    def test_inactive_gates_are_free(self):
        # out = AND(x0, x1); gate 1 is unconnected.
        g = Genome(2, [GATE_INDEX["AND"], GATE_INDEX["OR"]], [0, 0], [1, 1], [2])
        self.assertEqual(g.active_nodes(), [0])
        self.assertEqual(g.gate_count(), 1)

    def test_mutation_changes_phenotype_and_keeps_parent(self):
        rng = random.Random(2)
        parent = random_genome(4, 2, 30, rng)
        before = parent.phenotype_key()
        for _ in range(50):
            child = mutate_single_active(parent, rng)
            self.assertEqual(parent.phenotype_key(), before)
            self.assertNotEqual(child.phenotype_key(), before)


class SeedTests(unittest.TestCase):
    def test_ripple_carry_adder_is_correct(self):
        for n in (1, 2, 3, 4):
            t = adder(n)
            g = ripple_carry_adder(n, 5 * n + 10, random.Random(n))
            self.assertEqual(score(g, t), t.max_score)
            self.assertEqual(g.gate_count(), 5 * n)

    def test_textbook_alu4_is_correct(self):
        t = alu4()
        g = textbook_alu4(100, random.Random(0))
        self.assertEqual(score(g, t), t.max_score)
        self.assertEqual(g.gate_count(), 67)

    def test_verilog_export_mentions_all_ports(self):
        t = adder(1)
        v = to_verilog(ripple_carry_adder(1, 10, random.Random(0)), t.input_names, t.output_names, "fa")
        for port in t.input_names + t.output_names:
            self.assertIn(port, v)


class EvolutionTests(unittest.TestCase):
    def test_evolves_full_adder_from_random(self):
        t = adder(1)
        result = evolve(t, n_nodes=30, generations=20_000, rng=random.Random(42), stop_when_correct=True)
        self.assertIsNotNone(result.first_correct_generation)
        self.assertEqual(score(result.best, t), t.max_score)

    def test_seeded_run_never_loses_correctness_or_grows(self):
        t = adder(2)
        rng = random.Random(7)
        seed = ripple_carry_adder(2, 40, rng)
        result = evolve(t, n_nodes=40, generations=2_000, rng=rng, initial=seed)
        self.assertEqual(score(result.best, t), t.max_score)
        self.assertLessEqual(result.best.gate_count(), seed.gate_count())
        self.assertTrue(all(p.correct for p in result.history))

    def test_fitness_prefers_correctness_over_size(self):
        t = adder(1)
        good = ripple_carry_adder(1, 10, random.Random(0))
        tiny = Genome(3, [GATE_INDEX["XOR"]], [0], [1], [3, 3])  # 1 gate, wrong
        self.assertGreater(fitness_key(good, t), fitness_key(tiny, t))

    def test_history_reports_real_gate_count_before_correct(self):
        t = adder(3)
        result = evolve(t, n_nodes=30, generations=50, rng=random.Random(3))
        self.assertTrue(any(p.gates > 0 for p in result.history))

    def test_size_ignored_until_correct(self):
        t = adder(1)
        small = Genome(3, [GATE_INDEX["XOR"]], [0], [1], [3, 3])
        big = Genome(3, [GATE_INDEX["XOR"], GATE_INDEX["AND"], GATE_INDEX["OR"]],
                     [0, 3, 4], [1, 3, 4], [5, 5])  # chain of 3 gates, same output as `small`
        self.assertEqual(score(small, t), score(big, t))
        self.assertEqual(fitness_key(small, t), fitness_key(big, t))


if __name__ == "__main__":
    unittest.main()
