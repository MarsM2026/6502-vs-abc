/* Reference: run Klaus Dormann's functional test on perfect6502, the transistor-level
 * simulation of the real NMOS 6502 (visual6502 netlist).
 *
 * usage: klaus_ref image.bin start success max_halfcycles [trace.bin trace_halfcycles]
 * The optional trace records, after every half-cycle: AB (16 bit), DB (8), RW (1), clk0 (1)
 * as 4 bytes, for lockstep comparison with our gate-level conversion.
 *
 * Build (from the project root):
 *   clang -O2 -Ivendor/perfect6502 -o build/klaus_ref tools/ref6502/klaus_ref.c \
 *         vendor/perfect6502/perfect6502.c vendor/perfect6502/netlist_sim.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "types.h"
#include "perfect6502.h"

extern BOOL isNodeHigh(void *state, int nn);

int main(int argc, char **argv) {
  if (argc < 5) { fprintf(stderr, "usage: klaus_ref image start success max_halfcycles [trace n]\n"); return 2; }
  FILE *f = fopen(argv[1], "rb");
  if (!f || fread(memory, 1, 65536, f) != 65536) { fprintf(stderr, "cannot read image\n"); return 2; }
  fclose(f);
  unsigned start = strtoul(argv[2], 0, 0), success = strtoul(argv[3], 0, 0);
  unsigned long long max_half = strtoull(argv[4], 0, 0);
  FILE *tr = argc >= 7 ? fopen(argv[5], "wb") : NULL;
  unsigned long long tr_n = argc >= 7 ? strtoull(argv[6], 0, 0) : 0;
  memory[0xFFFC] = start & 0xFF; memory[0xFFFD] = start >> 8;

  void *s = initAndResetChip();
  unsigned ring[64]; memset(ring, 0xFF, sizeof ring);
  unsigned hits = 0;
  for (unsigned long long h = 0; h < max_half; h++) {
    step(s);
    unsigned ab = readAddressBus(s);
    if (tr && h < tr_n) {
      unsigned char rec[4] = { ab & 0xFF, ab >> 8, readDataBus(s), (unsigned char)(readRW(s) | (isNodeHigh(s, 1171) << 1)) };
      fwrite(rec, 1, 4, tr);
      if (h + 1 == tr_n) { fclose(tr); tr = NULL; }
    }
    if (h & 1) continue;                       /* check once per full cycle */
    unsigned long long cyc = h / 2;
    ring[cyc & 63] = ab;
    if (ab == success && ++hits >= 3) { printf("PASS cycles=%llu\n", cyc); return 0; }
    if ((cyc & 1023) == 0 && cyc > 1000) {
      for (int p = 1; p <= 8; p++) {
        int stuck = 1;
        for (int k = 0; k < 48 && stuck; k++) stuck = ring[(cyc - k) & 63] == ring[(cyc - k - p) & 63];
        if (stuck) { printf("TRAP cycles=%llu ab=0x%04X period=%d\n", cyc, ab, p); return 1; }
      }
    }
  }
  printf("TIMEOUT halfcycles=%llu\n", max_half);
  return 1;
}
