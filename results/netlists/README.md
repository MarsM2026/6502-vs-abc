# Tested netlists

Gate netlists of Arlet Ottens' 6502 core (`vendor/verilog-6502`) in this project's JSON
format (`src/netlist.py`: inputs, outputs, two-input gates, D flip-flops). Every file passes
Klaus Dormann's functional test with `python src/csim.py --netlist <file>`.

| File | Gates | How it was made |
|---|---:|---|
| `arlet_abc_plateau_1994.json` | 1,994 | Yosys + ABC default script, iterated to its plateau |
| `arlet_evolved_1963.json` | 1,963 | + window CGP with SAT and register-state don't-cares |
| `arlet_abc_deepsyn_1837.json` | 1,837 | ABC plateau + `&deepsyn` (10 min, then 2 × 5 min) |
| `arlet_evolved_deepsyn_1822.json` | **1,822** | evolved netlist + `&deepsyn` (10 min, then 5 min): best |
| `arlet_nmos_cells_4867.json` | 1,072 cells | NMOS cell netlist (`src/cells.py`), 4,867 transistors incl. flip-flops |
| `arlet_nmos_pass_4657.json` | 1,025 cells | NMOS cells with wide static gates and 134 pass-transistor muxes (`src/pass_rewrite.py`), **4,657 transistors** incl. flip-flops |
| `arlet_round3_invariants.json` | | 322 register-state invariants used for the 1,963 netlist |

The cell netlists are checked (same for the 4,657 file) with `python -c "import sys; sys.path.insert(0,'src'); from cells import *; from netlist import save_circuit; save_circuit(expand(load_cells('results/netlists/arlet_nmos_cells_4867.json')), 'build/cells_gates.json')"`
followed by `python src/csim.py --netlist build/cells_gates.json`.

Netlists of the real 6502 are not included: they are derived from the visual6502 netlist
(CC BY-NC-SA 3.0). Recreate them with `python src/real6502.py` (see docs/real6502.md).
