# Third-party material

Everything outside `vendor/` is MIT licensed (see `LICENSE`). The files in `vendor/` come
from other projects and keep their own licenses. Each folder has a `COMMIT` file with the
upstream commit it was copied from.

| Folder | What | Author | License |
|---|---|---|---|
| `vendor/verilog-6502/` | Verilog 6502 core (`cpu.v`, `ALU.v`) | Arlet Ottens | Free use, commercial or not, provided the copyright notice in the files is kept |
| `vendor/klaus-functional-tests/` | 6502 functional test (source, listing, binary) | Klaus Dormann | GNU GPL v3 (`license.txt`) |
| `vendor/perfect6502/` (C code) | Switch-level 6502 simulator | Michael Steil, Brian Silverman, Barry Silverman | MIT (`LICENSE`) |
| `vendor/perfect6502/netlist_6502.h` | Transistor netlist of the NMOS 6502 | Greg James, Brian Silverman, Barry Silverman / visual6502.org | **CC BY-NC-SA 3.0** |
| `vendor/visual6502/` | Node names (`nodenames.js`), transistor definitions | visual6502.org | MIT |

## Notes

* **Non-commercial data.** `netlist_6502.h` is licensed CC BY-NC-SA 3.0: non-commercial
  use only, credit Greg James and www.visual6502.org, and share adaptations under the same
  license. Netlists derived from it (`build/real6502*.json`, made by `src/real6502.py`)
  fall under the same terms. They are not included in this repository; run
  `python src/real6502.py` to generate them.
* **GPL test program.** Klaus Dormann's test is included unchanged as data for the
  simulators. It stays under the GPL v3; the MIT license of this project does not apply
  to it.
