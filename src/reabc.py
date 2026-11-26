"""Run ABC over one of our own netlists: export to Verilog, Yosys+ABC, load back.

Usage (from the project root):
  python src/reabc.py --netlist build/cpu_k15_dc.json --out build/cpu_reabc1.json
then validate:  python src/csim.py --netlist build/cpu_reabc1.json
Needs native Yosys from OSS CAD Suite (default location C:/tools/oss-cad-suite).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from netlist import cleanup, load_circuit, load_yosys_json, save_circuit, save_verilog

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--oss-cad", type=Path, default=Path("C:/tools/oss-cad-suite"))
    ap.add_argument("--abc", default=None,
                    help='replace the ABC command, e.g. "abc -dff -g AND,NAND,OR,NOR,XOR,XNOR"')
    ap.add_argument("--init-zero", action="store_true",
                    help="give every register initial value 0 (needed for scorr with abc -dff)")
    ap.add_argument("--tag", default="", help="suffix for the work files, so runs can overlap")
    args = ap.parse_args(argv)

    c = load_circuit(args.netlist)
    print("in: ", c.summary())
    script = (ROOT / "synth" / "reabc.ys").read_text()
    if args.abc:
        script = script.replace("abc -g AND,NAND,OR,NOR,XOR,XNOR", args.abc)
    script = script.replace("build/reabc_", f"build/reabc{args.tag}_")
    ys = ROOT / "build" / f"reabc{args.tag}.ys"
    ys.write_text(script)
    save_verilog(c, ROOT / "build" / f"reabc{args.tag}_in.v", init_zero=args.init_zero)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(args.oss_cad / "bin"), str(args.oss_cad / "lib"), env["PATH"]])
    subprocess.run([str(args.oss_cad / "bin" / "yosys"), "-q", "-s", str(ys)], cwd=ROOT, env=env, check=True)
    out = load_yosys_json(ROOT / "build" / f"reabc{args.tag}_out.json")
    cleanup(out)
    print("out:", out.summary())
    save_circuit(out, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
