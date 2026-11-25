"""Count our netlists in the same technology as the real 6502: NMOS transistors.

We write a Liberty cell library of NMOS static gates and let ABC map the combinational
logic onto it, minimising area. An NMOS static gate computes NOT(f) with f a
series/parallel network of k enhancement transistors plus one depletion pull-up, so
its area is k + 1 transistors (INV = 2, NAND2 = NOR2 = 3, AOI22 = 5, ...).

Flip-flops: the 6502 stores state in dynamic two-phase latches. A master-slave
flip-flop built that way is 2 pass transistors + 2 inverters = 6 transistors. That is
the cost we add per flip-flop (an estimate; the real chip also shares latches with
buses and precharge logic, see docs/real6502.md).

Usage (from the project root):
  python src/nmos_map.py                                  # Arlet's core from RTL, ABC mapping
  python src/nmos_map.py --netlist build/cpu_seqdc.json   # one of our optimised netlists
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

from netlist import load_circuit, save_verilog

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "build" / "nmos.lib"
DFF_TRANSISTORS = 6

# name -> Liberty function of the pull-down network (the cell output is its negation).
PULLDOWNS: dict[str, str] = {}
for k in range(1, 5):
    PULLDOWNS[f"NAND{k}" if k > 1 else "INV"] = "&".join("ABCD"[:k])
for k in range(2, 9):
    PULLDOWNS[f"NOR{k}"] = "|".join("ABCDEFGH"[:k])
PULLDOWNS.update({
    "AOI21": "(A&B)|C", "AOI22": "(A&B)|(C&D)", "AOI211": "(A&B)|C|D", "AOI221": "(A&B)|(C&D)|E",
    "AOI222": "(A&B)|(C&D)|(E&F)", "AOI31": "(A&B&C)|D", "AOI32": "(A&B&C)|(D&E)", "AOI33": "(A&B&C)|(D&E&F)",
    "OAI21": "(A|B)&C", "OAI22": "(A|B)&(C|D)", "OAI211": "(A|B)&C&D", "OAI31": "(A|B|C)&D",
    "OAI221": "(A|B)&(C|D)&E", "OAI222": "(A|B)&(C|D)&(E|F)", "OAI32": "(A|B|C)&(D|E)",
})


def write_liberty(path: Path = LIB) -> None:
    L = ["library(nmos6502) {", "  delay_model : table_lookup;", "  time_unit : \"1ns\";",
         "  capacitive_load_unit (1,pf);"]
    for name, f in PULLDOWNS.items():
        pins = sorted(set(re.findall(r"[A-H]", f)))
        L.append(f"  cell({name}) {{")
        L.append(f"    area : {len(re.findall(r'[A-H]', f)) + 1};")
        for p in pins:
            L.append(f"    pin({p}) {{ direction : input; capacitance : 1; }}")
        L.append(f"    pin(Y) {{ direction : output; function : \"!({f})\"; }}")
        L.append("  }")
    L.append("}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n")


MAPPED_JSON = ROOT / "build" / "nmos_mapped.json"
DEFAULT_SCRIPT = "+strash;dc2;&get,-n;&dch,-f;&put;map,-a"


def run_yosys(read_cmds: str, oss_cad: Path, structural: bool = False, script: str | None = None) -> str:
    """Area-only mapping (`map -a`): the library has no timing data, and we only
    care about transistor count. `structural` skips ABC's restructuring (dc2, dch),
    so the mapping follows the netlist's own structure."""
    abc_script = script or ("+strash;map,-a" if structural else DEFAULT_SCRIPT)
    stat_file = ROOT / "build" / "nmos_stat.txt"
    ys_file = ROOT / "build" / "nmos_map.ys"
    ys_file.write_text(f"""{read_cmds}
async2sync
dfflegalize -cell $_DFF_P_ 01
opt -full -nodffe -nosdff
abc -liberty {LIB.as_posix()} -script {abc_script}
opt_clean -purge
tee -q -o {stat_file.as_posix()} stat -liberty {LIB.as_posix()}
write_json {MAPPED_JSON.as_posix()}
""")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(oss_cad / "bin"), str(oss_cad / "lib"), env["PATH"]])
    subprocess.run([str(oss_cad / "bin" / "yosys"), "-q", "-s", str(ys_file)],
                   cwd=ROOT, env=env, check=True)
    return stat_file.read_text()


def parse_stat(out: str) -> tuple[float, int, dict[str, int]]:
    area = float(re.findall(r"Chip area for (?:top )?module.*?:\s*([\d.]+)", out)[-1])
    # rows look like "     165      660   AOI21" or "     143        -   $_DFF_P_"
    cells = {m[1]: int(m[0]) for m in re.findall(r"^\s+(\d+)\s+(?:[\d.]+|-)\s+-?\s*(\$_DFF_P_|[A-Z][A-Z0-9]*)\s*$", out, re.M)}
    dffs = cells.pop("$_DFF_P_", 0)
    return area, dffs, cells


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, default=None, help="our netlist (JSON); default: Arlet's RTL")
    ap.add_argument("--oss-cad", type=Path, default=Path("C:/tools/oss-cad-suite"))
    ap.add_argument("--structural", action="store_true",
                    help="map without ABC restructuring (measures the netlist's own structure)")
    ap.add_argument("--show", action="store_true", help="print the raw Yosys stat output")
    ap.add_argument("--script", default=None, help="ABC script (commas for spaces), overrides the default")
    args = ap.parse_args(argv)

    write_liberty()
    if args.netlist is None:
        read = "read_verilog vendor/verilog-6502/ALU.v vendor/verilog-6502/cpu.v\nsynth -top cpu -flatten"
        label = "Arlet RTL"
    else:
        save_verilog(load_circuit(args.netlist), ROOT / "build" / "nmos_in.v")
        read = "read_verilog build/nmos_in.v\nproc\nopt -nodffe -nosdff\ntechmap"
        label = str(args.netlist)
    if args.structural:
        label += " [structural]"
    if args.script:
        label += f" [{args.script}]"
    out = run_yosys(read, args.oss_cad, args.structural, args.script)
    if args.show:
        print(out)
    area, dffs, cells = parse_stat(out)
    total = area + DFF_TRANSISTORS * dffs
    print(f"{label}: combinational {area:.0f} NMOS transistors in {sum(cells.values())} cells, "
          f"{dffs} flip-flops x {DFF_TRANSISTORS} = {DFF_TRANSISTORS * dffs}, total {total:.0f}")
    print("  cells:", dict(sorted(cells.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
