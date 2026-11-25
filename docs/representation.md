# Circuit representation

## Decision: Cartesian Genetic Programming (CGP)

Candidates are encoded with CGP (Miller & Thomson, 2000), the standard representation
in evolvable-hardware research. A genome is a fixed-length list of integers:

```
[ func, in1, in2 ]  × n_nodes      one triple per gate
[ out ]             × n_outputs    which signal drives each primary output
```

Signals are addressed by number: `0 .. n_inputs-1` are primary inputs, and gate `i`
is signal `n_inputs + i`. Gate `i` may only read signals with a lower address, so
every genome is a valid feed-forward circuit and no mutation can create a
combinational loop.

Gate set: AND, OR, XOR, NAND, NOR, XNOR (2-input) and NOT (ignores `in2`).

### Active vs inactive gates

Only gates reachable backwards from an output are *active*. Inactive gates cost
nothing (they aren't counted in the gate total) but stay in the genome. Mutations
to them are neutral, and that hidden material later gets reconnected. This
*neutral drift* is the main reason CGP searches well, and it's why the default
genome (100 nodes) is several times larger than the circuits it evolves.

## Simulation: bit-parallel, exhaustive

Each signal is a single Python integer whose bit *v* is the signal's value for
input vector *v*. One integer operation per gate evaluates the circuit for **every**
input combination at once (512 for a 4-bit adder, 2048 for the ALU slice). So
fitness is exact rather than sampled: a circuit scoring 100% is proven correct.

Above about 20 inputs the truth tables get too large. The 6502 stage will need
SAT-based equivalence checking for big blocks (see the README roadmap).

## Search: (1 + λ) evolution strategy

- One parent, λ = 4 children per generation, each made with *single-active-gene mutation*
  (keep mutating random genes until one that changes the circuit is hit).
- The best child replaces the parent if it is **at least as good**. Accepting ties allows neutral drift.
- No crossover. Netlist crossover tends to break the circuit's wiring, and in the
  CGP literature it rarely beats mutation alone.

## Fitness

Lexicographic `(correct output bits, −active gates)`:

1. Correctness always dominates.
2. Among **fully** correct circuits, fewer gates wins. Before full correctness the
   gate count is ignored (two-phase fitness, see results below).

As a result a correct circuit can never be replaced by an incorrect one, and once
correctness is reached the search becomes pure gate-count minimisation. Seeded runs
use exactly this property: start from a known-correct circuit and only shrink it.

## First results (2026-11-22, single runs, `--seed 1`)

| Run | Outcome |
|-----|---------|
| `adder2`, from random, 50k generations | correct after 26k generations (12.6 s); shrank to 14 gates (ripple-carry: 10) |
| `adder4`, seeded with 20-gate ripple-carry, 50k generations | stayed correct; no reduction below 20 gates |
| `adder4`, from random, size penalised from the start | **failed**: stuck at 86% correct, circuits held at 13–20 gates |
| `adder4`, from random, two-phase fitness | correct after 295k generations (1.18M evaluations, 281 s), 38 gates, shrank to 33 |

### `adder4` from random, two-phase fitness, 5 seeds (400k generations, 100 nodes, λ = 4)

| Seed | First correct (generation) | Evaluations | Time | Gates when first correct | Final gates |
|-----:|------:|------:|------:|-----:|-----:|
| 1 | 294,529 | 1,178,117 | 281 s | 38 | 33 |
| 2 | 123,683 | 494,733 | 124 s | 36 | 28 |
| 3 | 118,643 | 474,573 | 107 s | 42 | 36 |
| 4 | 227,525 | 910,101 | 181 s | 38 | 31 |
| 5 | 117,705 | 470,821 | 88 s | 39 | 22 |

5/5 runs reached a correct 4-bit adder (median ~495k evaluations). Correct circuits
start bloated (36–42 gates) and shrink, but none matched the 20-gate ripple-carry
design within the budget (best: 22). Times are wall-clock while other jobs shared the CPU.

### `alu4` from random, seed 1 (1M generations, 200 nodes, λ = 4, 28 min)

**Not solved**: 99.28% of output bits correct, 82 gates. All outputs are perfect
except `r3` (the top result bit) during ADC, which is wrong in 88 of 512 cases.
The circuit sits on a high plateau where fixing the last carry chain would
require rewiring that temporarily breaks bits it already gets right. This is a
small example of the deceptive fitness that makes a whole CPU from random gates unrealistic.

### `alu4` seeded with a 67-gate textbook ALU, 3 seeds (300k generations, 150 nodes, ~7 min each)

All three seeds reduced it to **58 gates (−13%)**, staying correct throughout. Each
got there within ~18k generations and then made no further progress. The main
trick evolution found: because `a OR b = (a XOR b) OR (a AND b)`, the ORA/EOR
choice needs only `x OR (AND-gated-by-op)`, which reuses the adder's XOR and AND
and drops the separate OR gate plus a 2:1 mux per bit. It also folded the
"carry only for ADC" gating into the carry chain itself.

All three runs hitting exactly 58 suggests a strong local optimum for this
starting design, not necessarily the minimum.

The failed run led to the two-phase fitness above: penalising size before the
circuit is correct kept candidates too small to ever implement the function.
These are single runs, so the numbers are anecdotes, not statistics.

## Hypotheses to test with this stage

- From random, CGP finds a correct 2-bit adder quickly and a 4-bit adder within ~10⁵–10⁶ evaluations.
- Seeded runs reach a correct small circuit far faster than random starts.
- The 20-gate ripple-carry adder is already near-minimal for this gate set. A
  noticeably smaller *evolved* 4-bit adder would be a surprising result worth checking.
- The ALU slice (11 inputs, 6 outputs) is noticeably harder from random. It is a
  first taste of why the full CPU must be seeded.
