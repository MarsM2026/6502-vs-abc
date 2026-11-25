"""Tests for the Yosys-JSON netlist loader. Run: python -m unittest discover -s tests"""

import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netlist import load_yosys_json  # noqa: E402


def write_module(ports, cells):
    mod = {"modules": {"cpu": {"ports": ports, "cells": cells, "netnames": {}}}}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(mod, f)
    f.close()
    return f.name


def cell(t, **pins):
    return {"type": t, "connections": {k: [v] for k, v in pins.items()}}


def simulate(circuit, values):
    s = [0] * circuit.n_signals
    s[1] = 1
    for sig, v in values.items():
        s[sig] = v
    for g in circuit.gates:
        a, b = s[g.a], s[g.b]
        s[g.y] = {"AND": a & b, "OR": a | b, "XOR": a ^ b, "NAND": 1 - (a & b), "NOR": 1 - (a | b),
                  "XNOR": 1 - (a ^ b), "NOT": 1 - a}[g.op]
    return s


class NetlistTests(unittest.TestCase):
    def test_mux_expands_to_gates_and_behaves(self):
        ports = {"a": {"direction": "input", "bits": [2]}, "b": {"direction": "input", "bits": [3]},
                 "sel": {"direction": "input", "bits": [4]}, "y": {"direction": "output", "bits": [5]}}
        c = load_yosys_json(write_module(ports, {"m": cell("$_MUX_", A=2, B=3, S=4, Y=5)}))
        self.assertTrue(all(g.op in ("AND", "OR", "NOT") for g in c.gates))
        a, b, sel, y = c.inputs["a"][0], c.inputs["b"][0], c.inputs["sel"][0], c.outputs["y"][0]
        for va, vb, vs in itertools.product((0, 1), repeat=3):
            s = simulate(c, {a: va, b: vb, sel: vs})
            self.assertEqual(s[y], vb if vs else va)

    def test_buffer_is_aliased_and_constants_mapped(self):
        ports = {"a": {"direction": "input", "bits": [2]}, "y": {"direction": "output", "bits": [4]}}
        cells = {"buf": cell("$_BUF_", A=2, Y=3), "and": cell("$_AND_", A=3, B="1", Y=4)}
        c = load_yosys_json(write_module(ports, cells))
        self.assertEqual(c.gate_count, 1)
        g = c.gates[0]
        self.assertEqual({g.a, g.b}, {c.inputs["a"][0], 1})

    def test_gates_are_topologically_sorted_and_dffs_break_cycles(self):
        # q -> NOT -> d -> DFF -> q  (a toggle flip-flop): legal, because the DFF breaks the loop.
        ports = {"clk": {"direction": "input", "bits": [2]}, "q": {"direction": "output", "bits": [3]}}
        cells = {"ff": cell("$_DFF_P_", C=2, D=4, Q=3), "inv": cell("$_NOT_", A=3, Y=4)}
        c = load_yosys_json(write_module(ports, cells))
        self.assertEqual(len(c.dffs), 1)
        self.assertEqual(c.gate_count, 1)

    def test_combinational_loop_is_rejected(self):
        ports = {"y": {"direction": "output", "bits": [2]}}
        cells = {"n1": cell("$_NOT_", A=2, Y=3), "n2": cell("$_NOT_", A=3, Y=2)}
        with self.assertRaises(ValueError):
            load_yosys_json(write_module(ports, cells))

    def test_unknown_cell_type_is_rejected(self):
        ports = {"y": {"direction": "output", "bits": [2]}}
        with self.assertRaises(ValueError):
            load_yosys_json(write_module(ports, {"x": cell("$_DFFE_PP_", C=2, D=2, E=2, Q=2)}))


if __name__ == "__main__":
    unittest.main()
