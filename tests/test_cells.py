"""Tests for NMOS cell netlists and cell-level window CGP. Run: python -m unittest discover -s tests"""

import itertools
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cells import ALL, CELLS, CellInst, CellNet, expand, topo_sort  # noqa: E402
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
        v[c.y] = ALL[c.type].fn([v[s] for s in c.ins], 1)
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

    def test_wide_and_pass_cells(self):
        # NOR16: 16 parallel pull-downs + load = 17; AOI2222 = 9; MUX2I = !(C ? A : B), area 4 + shared select inverter
        self.assertEqual(ALL["NOR16"].area, 17)
        self.assertEqual(ALL["AOI2222"].area, 9)
        self.assertEqual(ALL["MUX2I"].area, 4)
        for a, b, c in itertools.product((0, 1), repeat=3):
            self.assertEqual(ALL["MUX2I"].fn([a, b, c], 1), 1 - (a if c else b))
        # bit-parallel evaluation stays inside the mask despite the inverted pin
        self.assertEqual(ALL["MUX2I"].fn([0b0011, 0b0101, 0b0000], 0b1111), 0b1010)

    def test_mux_select_cost_counts_each_select_once(self):
        from nmos_map import mux_select_cost
        # two muxes on select 5, one on select 6; signal 5 is already inverted by an INV
        cn = CellNet({"x": [2, 3, 4, 5, 6]}, {"y": [9, 10, 11]},
                     [CellInst("INV", [5], 7), CellInst("MUX2I", [2, 3, 5], 9),
                      CellInst("MUX2I", [3, 4, 5], 10), CellInst("MUX2I", [2, 4, 6], 11)], [], 12)
        self.assertEqual(mux_select_cost(cn), 2)
        # a select that is itself the output of an INV also has its inverse available
        cn.cells.append(CellInst("MUX2I", [2, 3, 7], 12))
        self.assertEqual(mux_select_cost(cn), 2)

    def test_pass_rewrite_preserves_function(self):
        from pass_rewrite import use_pass_muxes
        rng = random.Random(9)
        names = ["INV", "AOI22", "OAI22", "NAND2"]
        replaced = 0
        for _ in range(40):
            ins = list(range(2, 7))
            sigs, cells, nxt = list(ins), [], 7
            for _ in range(12):
                t = rng.choice(names)
                cells.append(CellInst(t, [rng.choice(sigs) for _ in range(ALL[t].arity)], nxt))
                sigs.append(nxt)
                nxt += 1
            cn = CellNet({"x": ins}, {"y": sigs[5:]}, cells, [], nxt)
            topo_sort(cn)
            table = lambda: [[eval_cells(cn, dict(zip(ins, b)))[o] for o in cn.outputs["y"]]
                             for b in itertools.product((0, 1), repeat=5)]
            ref = table()
            replaced += use_pass_muxes(cn)
            self.assertEqual(ref, table())
        self.assertGreater(replaced, 0)

    def test_expand_matches_cells(self):
        """Every cell of every library variant, expanded to two-input gates, computes the same function."""
        for name, cell in ALL.items():
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
