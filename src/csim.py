"""Compile a gate-level 6502 netlist to C and run Klaus Dormann's functional test.

Why C: the test runs tens of millions of clock cycles over ~3,000 gates. Pure
Python would take hours per run; straight-line C takes about a minute. This is
the documented exception to the project's "plain Python" rule.

Memory model (matches Arlet Ottens' core, which expects *synchronous* RAM):
on every rising clock edge the RAM latches DI <= mem[AB] and, if WE, writes
mem[AB] <= DO.

Usage (from the project root):
  python src/csim.py                     # synthesised netlist, full functional test
  python src/csim.py --max-cycles 100000 # quick smoke run
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from netlist import Circuit, load_circuit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NETLIST = ROOT / "build" / "cpu_gates.json"
DEFAULT_BIN = ROOT / "vendor" / "klaus-functional-tests" / "6502_functional_test.bin"
START, SUCCESS = 0x0400, 0x3469

_EXPR = {
    "AND": "s[{a}] & s[{b}]", "OR": "s[{a}] | s[{b}]", "XOR": "s[{a}] ^ s[{b}]",
    "NAND": "!(s[{a}] & s[{b}])", "NOR": "!(s[{a}] | s[{b}])", "XNOR": "!(s[{a}] ^ s[{b}])",
    "NOT": "!s[{a}]",
}


def generate_c(c: Circuit) -> str:
    def bus(name: str, bits: list[int]) -> str:
        return " | ".join(f"(s[{b}] << {i})" for i, b in enumerate(bits)) or "0"

    L = []
    L.append("#include <stdint.h>\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n")
    L.append(f"#define NSIG {c.n_signals}\n#define NDFF {len(c.dffs)}")
    L.append("static uint8_t s[NSIG];\nstatic uint8_t mem[65536];\n")
    L.append("static void eval(void) {")
    for g in c.gates:
        L.append(f"  s[{g.y}] = {_EXPR[g.op].format(a=g.a, b=g.b)};")
    L.append("}\n")
    L.append("static void clock(void) {\n  static uint8_t n[NDFF > 0 ? NDFF : 1];")
    for i, (d, _) in enumerate(c.dffs):
        L.append(f"  n[{i}] = s[{d}];")
    for i, (_, q) in enumerate(c.dffs):
        L.append(f"  s[{q}] = n[{i}];")
    L.append("}\n")

    inp = c.inputs
    L.append("static void set_inputs(int reset, uint8_t di) {")
    L.append(f"  s[{inp['reset'][0]}] = reset; s[{inp['RDY'][0]}] = 1;")
    L.append(f"  s[{inp['IRQ'][0]}] = 0; s[{inp['NMI'][0]}] = 0; s[{inp['clk'][0]}] = 0;")
    for i, b in enumerate(inp["DI"]):
        L.append(f"  s[{b}] = (di >> {i}) & 1;")
    L.append("}")
    L.append(f"static unsigned get_ab(void) {{ return {bus('AB', c.outputs['AB'])}; }}")
    L.append(f"static unsigned get_do(void) {{ return {bus('DO', c.outputs['DO'])}; }}")
    L.append(f"static unsigned get_we(void) {{ return s[{c.outputs['WE'][0]}]; }}\n")

    L.append(r"""
int main(int argc, char **argv) {
  if (argc < 5) { fprintf(stderr, "usage: sim image.bin start success max_cycles\n"); return 2; }
  FILE *f = fopen(argv[1], "rb");
  if (!f || fread(mem, 1, 65536, f) != 65536) { fprintf(stderr, "cannot read %s\n", argv[1]); return 2; }
  fclose(f);
  unsigned start = strtoul(argv[2], 0, 0), success = strtoul(argv[3], 0, 0);
  unsigned long long max_cycles = strtoull(argv[4], 0, 0);
  mem[0xFFFC] = start & 0xFF; mem[0xFFFD] = start >> 8;   /* reset vector -> test start */
  s[1] = 1;
  uint8_t di = 0;
  unsigned ring[64]; memset(ring, 0xFF, sizeof ring);
  unsigned success_hits = 0;
  for (unsigned long long cyc = 0; cyc < max_cycles; cyc++) {
    int reset = cyc < 8;
    set_inputs(reset, di);
    eval();
    unsigned ab = get_ab(), dout = get_do(), we = get_we();
    /* rising edge: synchronous RAM and all flip-flops */
    uint8_t next_di = mem[ab];
    if (we && !reset) mem[ab] = dout;
    clock();
    di = next_di;
    if (reset) continue;
    ring[cyc & 63] = ab;
    if (ab == success && ++success_hits >= 3) {
      printf("PASS cycles=%llu\n", cyc); return 0;
    }
    if ((cyc & 1023) == 0 && cyc > 1000) {          /* detect a CPU stuck in a trap loop */
      for (int p = 1; p <= 8; p++) {
        int stuck = 1;
        for (int k = 0; k < 48 && stuck; k++)
          stuck = ring[(cyc - k) & 63] == ring[(cyc - k - p) & 63];
        if (stuck) { printf("TRAP cycles=%llu ab=0x%04X period=%d\n", cyc, ab, p); return 1; }
      }
    }
  }
  printf("TIMEOUT cycles=%llu\n", max_cycles);
  return 1;
}
""")
    return "\n".join(L)


def find_cc() -> str:
    for cc in ("clang", "gcc", "cc"):
        if shutil.which(cc):
            return cc
    raise RuntimeError("no C compiler found (need clang or gcc)")


def build(c: Circuit, out_dir: Path, name: str = "cpu_sim") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / f"{name}.c"
    exe = out_dir / (name + (".exe" if sys.platform == "win32" else ""))
    src.write_text(generate_c(c))
    subprocess.run([find_cc(), "-O1", "-o", str(exe), str(src)], check=True)
    return exe


def run(exe: Path, image: Path, max_cycles: int) -> tuple[bool, str]:
    p = subprocess.run([str(exe), str(image), hex(START), hex(SUCCESS), str(max_cycles)],
                       capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0 and out.startswith("PASS"), out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, default=DEFAULT_NETLIST)
    ap.add_argument("--image", type=Path, default=DEFAULT_BIN)
    ap.add_argument("--max-cycles", type=int, default=200_000_000)
    ap.add_argument("--exe-name", default="cpu_sim", help="name of the generated C file / executable in build/")
    args = ap.parse_args(argv)

    circuit = load_circuit(args.netlist)  # Yosys JSON or our own saved format
    print("netlist:", circuit.summary())
    t0 = time.perf_counter()
    exe = build(circuit, ROOT / "build", args.exe_name)
    print(f"compiled in {time.perf_counter() - t0:.1f}s -> {exe.relative_to(ROOT)}")
    t0 = time.perf_counter()
    ok, out = run(exe, args.image, args.max_cycles)
    print(f"{out}  ({time.perf_counter() - t0:.1f}s)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
