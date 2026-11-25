"""Tests for NMOS cell netlists and cell-level window CGP. Run: python -m unittest discover -s tests"""

import itertools
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cells import CELLS, CellInst, CellNet, expand, topo_sort  # noqa: E402
from cell_windows import optimise  # noqa: E402

PINS = "ABCDEFGH"


def eval_gates(c, env):
    v = dict(env)
    v[0], v[1] = 0, 1
    for g in c.gates:
        a, b = v[g.a], v[g.b]
        v[g.y] = {"AND": a & b, "OR": a | b, "XOR": a ^ b, "NAND": 1 - (a & b), "NOR": 1 - (a | b),
                  "XNOR": 1 - (a ^ b), "NOT": 1 - a}[g.op]
    return v


def eval_cells(cn, env):
    v = dict(env)
    v[0], v[1] = 0, 1
    for c in cn.cells:
        v[c.y] = CELLS[c.type].fn([v[s] for s in c.ins], 1)
    return v


def random_cellnet(n_in, n_cells, n_out, rng):
    ins = list(range(2, 2 + n_in))
    sigs, cells, nxt = list(ins), [], 2 + n_in
    names = sorted(CELLS)
    for _ in range(n_cells):
        t = rng.choice(names)
        cells.append(CellInst(t, [rng.choice(sigs) for _ in range(CELLS[t].arity)], nxt))
        sigs.append(nxt)
        nxt += 1
    cn = CellNet({"x": ins}, {"y": rng.sample(sigs[n_in:], n_out)}, cells, [], nxt)
    topo_sort(cn)
    return cn


class CellTests(unittest.TestCase):
    def test_cell_functions_and_areas(self):
        # AOI21 = !((A&B)|C), area 4; OAI22 = !((A|B)&(C|D)), area 5; INV area 2
        self.assertEqual(CELLS["AOI21"].area, 4)
        self.assertEqual(CELLS["OAI22"].area, 5)
        self.assertEqual(CELLS["INV"].area, 2)
        for a, b, c in itertools.product((0, 1), repeat=3):
            self.assertEqual(CELLS["AOI21"].fn([a, b, c], 1), 1 - ((a & b) | c))
            self.assertEqual(CELLS["NOR3"].fn([a, b, c], 1), 1 - (a | b | c))

    def test_expand_matches_cells(self):
        """Every library cell, expanded to two-input gates, computes the same function."""
        for name, cell in CELLS.items():
            ins = list(range(2, 2 + cell.arity))
            cn = CellNet({"x": ins}, {"y": [100]}, [CellInst(name, ins, 100)], [], 101)
            c = expand(cn)
            for bits in itertools.product((0, 1), repeat=cell.arity):
                env = dict(zip(ins, bits))
                self.assertEqual(eval_gates(c, env)[100], cell.fn(list(bits), 1), name)

    def test_window_optimisation_preserves_function_and_lowers_area(self):
        rng = random.Random(5)
        for trial in range(3):
            cn = random_cellnet(6, 25, 3, rng)
            ref = [[eval_cells(cn, dict(zip(cn.inputs["x"], bits)))[o] for o in cn.outputs["y"]]
                   for bits in itertools.product((0, 1), repeat=6)]
            before = cn.area
            optimise(cn, 1, 6, 8, 300, random.Random(trial), dont_cares=True, log=lambda *a: None)
            self.assertLessEqual(cn.area, before)
            got = [[eval_cells(cn, dict(zip(cn.inputs["x"], bits)))[o] for o in cn.outputs["y"]]
                   for bits in itertools.product((0, 1), repeat=6)]
            self.assertEqual(ref, got)


if __name__ == "__main__":
    unittest.main()
