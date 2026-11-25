"""Command-line runner for gate-level circuit evolution.

Examples (run from the project root):
  python src/evolve_circuit.py --target adder2                 # from random, quick
  python src/evolve_circuit.py --target adder4 --gens 200000   # from random, slower
  python src/evolve_circuit.py --target adder4 --seeded        # optimise a ripple-carry adder
  python src/evolve_circuit.py --target alu4 --nodes 200 --gens 500000
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from cgp import to_netlist, to_verilog
from circuit_evolution import Progress, evolve
from seeds import SEEDS
from targets import TARGETS

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evolve logic-gate circuits with CGP.")
    ap.add_argument("--target", choices=sorted(TARGETS), default="adder2")
    ap.add_argument("--nodes", type=int, default=100, help="max gates in the genome (default 100)")
    ap.add_argument("--lam", type=int, default=4, help="children per generation (default 4)")
    ap.add_argument("--gens", type=int, default=100_000, help="generations (default 100000)")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    ap.add_argument("--seeded", action="store_true", help="start from a hand-built correct circuit")
    ap.add_argument("--stop-when-correct", action="store_true",
                    help="stop at the first fully correct circuit instead of shrinking it further")
    ap.add_argument("--no-save", action="store_true", help="don't write results/ files")
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randrange(2**31)
    rng = random.Random(seed)
    target = TARGETS[args.target]()

    initial = None
    if args.seeded:
        if args.target not in SEEDS:
            ap.error(f"no hand-built seed for {args.target}; available: {', '.join(sorted(SEEDS))}")
        initial = SEEDS[args.target](args.nodes, rng)

    print(f"target={target.name} inputs={target.n_inputs} outputs={target.n_outputs} "
          f"vectors={target.n_vectors} nodes={args.nodes} lambda={args.lam} "
          f"seed={seed} start={'seeded' if initial else 'random'}")

    def show(p: Progress) -> None:
        pct = 100.0 * p.score / p.max_score
        flag = "  CORRECT" if p.correct else ""
        print(f"gen {p.generation:>8}  evals {p.evaluations:>9}  correct {pct:6.2f}%  "
              f"gates {p.gates:>4}  {p.elapsed:7.1f}s{flag}")

    result = evolve(target, n_nodes=args.nodes, lam=args.lam, generations=args.gens, rng=rng,
                    initial=initial, stop_when_correct=args.stop_when_correct, on_progress=show)

    best = result.best
    final = result.history[-1]
    print()
    if result.first_correct_generation is None:
        print(f"No fully correct circuit found ({100.0 * final.score / final.max_score:.2f}% of bits correct).")
    else:
        print(f"First correct circuit at generation {result.first_correct_generation}; "
              f"final size {best.gate_count()} gates.")
    print()
    print(to_netlist(best, target.input_names, target.output_names))

    if not args.no_save:
        RESULTS_DIR.mkdir(exist_ok=True)
        stem = f"{target.name}-{'seeded' if initial else 'random'}-{seed}"
        (RESULTS_DIR / f"{stem}.v").write_text(
            to_verilog(best, target.input_names, target.output_names, target.name) + "\n")
        (RESULTS_DIR / f"{stem}.json").write_text(json.dumps({
            "target": target.name, "seed": seed, "seeded": bool(initial),
            "nodes": args.nodes, "lambda": args.lam, "generations": final.generation,
            "evaluations": final.evaluations, "correct": final.correct,
            "first_correct_generation": result.first_correct_generation,
            "gates": best.gate_count(),
            "genome": {"n_inputs": best.n_inputs, "funcs": best.funcs, "in1": best.in1,
                       "in2": best.in2, "outputs": best.outputs},
            "history": [p.__dict__ for p in result.history],
        }, indent=1))
        print(f"\nSaved results/{stem}.v and results/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
