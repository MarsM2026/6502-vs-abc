"""Simulate the gate-level conversion of the real 6502 (src/real6502.py) in C.

Same bus protocol as perfect6502: every step toggles clk0 and lets the chip settle; on the
rising edge of clk0 the memory is served (read: the data pins are forced to mem[AB] and the
chip settles again; write: mem[AB] = data pins). "Settle" = repeat one settling step
(evaluate all gates, copy every prev register) until no register changes.

Modes:
  python src/csim_real.py --compare build/ref_trace.bin    # lockstep vs perfect6502 trace
  python src/csim_real.py                                  # Klaus Dormann's functional test
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from csim import DEFAULT_BIN, START, SUCCESS, find_cc
from netlist import Circuit, load_circuit

ROOT = Path(__file__).resolve().parent.parent
_EXPR = {
    "AND": "s[{a}] & s[{b}]", "OR": "s[{a}] | s[{b}]", "XOR": "s[{a}] ^ s[{b}]",
    "NAND": "!(s[{a}] & s[{b}])", "NOR": "!(s[{a}] | s[{b}])", "XNOR": "!(s[{a}] ^ s[{b}])",
    "NOT": "!s[{a}]",
}


def generate_c(c: Circuit) -> str:
    def bus(bits):
        return " | ".join(f"((unsigned)s[{b}] << {i})" for i, b in enumerate(bits))

    i = c.inputs
    L = ["#include <stdint.h>\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>",
         f"#define NSIG {c.n_signals}\n#define NDFF {len(c.dffs)}",
         "static uint8_t s[NSIG]; static uint8_t mem[65536];",
         "static unsigned long long settle_steps, settle_calls, settle_max, not_settled;",
         "static void eval(void) {"]
    L += [f"  s[{g.y}] = {_EXPR[g.op].format(a=g.a, b=g.b)};" for g in c.gates]
    L.append("}")
    L.append("static int clock(void) {\n  static uint8_t n[NDFF > 0 ? NDFF : 1]; int ch = 0;")
    L += [f"  n[{k}] = s[{d}];" for k, (d, _) in enumerate(c.dffs)]
    L += [f"  ch |= s[{q}] ^ n[{k}]; s[{q}] = n[{k}];" for k, (_, q) in enumerate(c.dffs)]
    L.append("  return ch;\n}")
    L.append(r"""
static void settle(void) {
  unsigned k;
  /* when clock() reports no change, the last eval() already saw stable registers */
  for (k = 1; k <= 400; k++) { eval(); if (!clock()) break; }
  if (k > 400) { not_settled++; eval(); }
  settle_steps += k; settle_calls++; if (k > settle_max) settle_max = k;
}""")
    L.append("static void set_pin(int pin, int v) { s[pin] = v; }")
    for p in ("res", "rdy", "so", "irq", "nmi", "clk0"):
        L.append(f"#define PIN_{p} {i[p][0]}")
    L.append("static const int PIN_DB[8] = {" + ", ".join(str(i[f'db{k}'][0]) for k in range(8)) + "};")
    o = c.outputs
    L.append(f"static unsigned ab(void) {{ return {bus([o[f'ab{k}'][0] for k in range(16)])}; }}")
    L.append(f"static unsigned db(void) {{ return {bus([o[f'dbo{k}'][0] for k in range(8)])}; }}")
    L.append(f"static unsigned rw(void) {{ return s[{o['rw'][0]}]; }}")
    L.append(r"""
static void write_db(unsigned d) {
  int ch = 0;
  for (int k = 0; k < 8; k++) { ch |= s[PIN_DB[k]] != ((d >> k) & 1); set_pin(PIN_DB[k], (d >> k) & 1); }
  if (ch) settle();
}
static void step(void) {
  int clk = s[PIN_clk0];
  set_pin(PIN_clk0, !clk); settle();
  if (!clk) { if (rw()) write_db(mem[ab()]); else mem[ab()] = db(); }
}

int main(int argc, char **argv) {
  if (argc < 6) { fprintf(stderr, "usage: sim image start success max_half [compare.bin]\n"); return 2; }
  FILE *f = fopen(argv[1], "rb");
  if (!f || fread(mem, 1, 65536, f) != 65536) { fprintf(stderr, "cannot read image\n"); return 2; }
  fclose(f);
  unsigned start = strtoul(argv[2], 0, 0), success = strtoul(argv[3], 0, 0);
  unsigned long long max_half = strtoull(argv[4], 0, 0);
  FILE *cmp = (argc > 5 && strcmp(argv[5], "-")) ? fopen(argv[5], "rb") : NULL;
  mem[0xFFFC] = start & 0xFF; mem[0xFFFD] = start >> 8;
  s[1] = 1;
  set_pin(PIN_res, 0); set_pin(PIN_clk0, 1); set_pin(PIN_rdy, 1); set_pin(PIN_so, 0);
  set_pin(PIN_irq, 1); set_pin(PIN_nmi, 1);
  settle();
  for (int k = 0; k < 16; k++) step();
  set_pin(PIN_res, 1); settle();

  unsigned ring[64]; memset(ring, 0xFF, sizeof ring);
  unsigned hits = 0; unsigned long long mismatches = 0, reset_mismatches = 0;
  const unsigned long long RESET_GRACE = 100;
  for (unsigned long long h = 0; h < max_half; h++) {
    step();
    unsigned a = ab();
    if (cmp) {
      unsigned char r[4];
      if (fread(r, 1, 4, cmp) != 4) { printf("COMPARE-END half=%llu mismatches=%llu\n", h, mismatches); break; }
      unsigned ra = r[0] | r[1] << 8, rd = r[2], rrw = r[3] & 1, rclk = r[3] >> 1;
      if (ra != a || rd != db() || rrw != rw() || rclk != s[PIN_clk0]) {
        /* The first half-cycles after reset show power-on register contents (e.g. the
           stack pointer), which are undefined in a real 6502: count them separately. */
        if (h < RESET_GRACE) reset_mismatches++;
        else {
          if (mismatches < 10)
            printf("MISMATCH half=%llu ref ab=%04X db=%02X rw=%u clk=%u | ours ab=%04X db=%02X rw=%u clk=%u\n",
                   h, ra, rd, rrw, rclk, a, db(), rw(), s[PIN_clk0]);
          mismatches++;
        }
      }
      continue;
    }
    if (h & 1) continue;
    unsigned long long cyc = h / 2;
    ring[cyc & 63] = a;
    if (a == success && ++hits >= 3) {
      printf("PASS cycles=%llu settle_avg=%.2f settle_max=%llu not_settled=%llu\n", cyc,
             (double)settle_steps / settle_calls, settle_max, not_settled); return 0; }
    if ((cyc & 1023) == 0 && cyc > 1000) {
      for (int p = 1; p <= 8; p++) {
        int stuck = 1;
        for (int k = 0; k < 48 && stuck; k++) stuck = ring[(cyc - k) & 63] == ring[(cyc - k - p) & 63];
        if (stuck) { printf("TRAP cycles=%llu ab=0x%04X period=%d\n", cyc, a, p); return 1; }
      }
    }
  }
  printf("END settle_avg=%.2f settle_max=%llu not_settled=%llu mismatches=%llu (first %llu half-cycles: %llu)\n",
         (double)settle_steps / settle_calls, settle_max, not_settled, mismatches, RESET_GRACE, reset_mismatches);
  return cmp ? (mismatches != 0) : 1;
}
""")
    return "\n".join(L)


def build(c: Circuit, name: str) -> Path:
    src = ROOT / "build" / f"{name}.c"
    exe = ROOT / "build" / (name + (".exe" if sys.platform == "win32" else ""))
    src.write_text(generate_c(c))
    subprocess.run([find_cc(), "-O1", "-o", str(exe), str(src)], check=True)
    return exe


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--netlist", type=Path, default=ROOT / "build" / "real6502.json")
    ap.add_argument("--compare", type=Path, default=None, help="perfect6502 trace from tools/ref6502")
    ap.add_argument("--max-half", type=int, default=400_000_000, help="max half-cycles")
    ap.add_argument("--exe-name", default="real_sim")
    args = ap.parse_args(argv)
    c = load_circuit(args.netlist)
    print("netlist:", c.summary())
    t0 = time.perf_counter()
    exe = build(c, args.exe_name)
    print(f"compiled in {time.perf_counter() - t0:.0f}s")
    t0 = time.perf_counter()
    cmd = [str(exe), str(DEFAULT_BIN), hex(START), hex(SUCCESS), str(args.max_half),
           str(args.compare) if args.compare else "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    print((p.stdout + p.stderr).strip(), f"({time.perf_counter() - t0:.1f}s)")
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
