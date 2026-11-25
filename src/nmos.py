"""Analyse the real NMOS 6502 transistor netlist (visual6502 / perfect6502).

The original 6502 (MOS Technology, 1975) is NMOS logic:
  * a *static gate* is a node with a depletion-mode pull-up (a resistor to VCC) and a
    pull-down network of enhancement transistors to ground. The node is 0 when the
    network conducts, so the gate computes NOT(f) where f is the series (AND) /
    parallel (OR) combination of the transistors' gate signals;
  * *pass transistors* connect two non-power nodes: latches (charge stored on a node,
    clocked by the two-phase clock), multiplexers and the internal buses;
  * transistors to VCC are push-pull output drivers ("superbuffers").

This script classifies every transistor that way and expresses the combinational part in
the same unit as our netlists: a static gate whose pull-down network has k transistors
needs k-1 two-input gates (k = 1: one NOT).

Data: vendor/perfect6502/netlist_6502.h, Copyright (c) 2010 Greg James, Brian Silverman,
Barry Silverman, www.visual6502.org, CC BY-NC-SA 3.0. Read as data only.

Usage (from the project root):  python src/nmos.py
"""

from __future__ import annotations

import collections
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NETLIST = ROOT / "vendor" / "perfect6502" / "netlist_6502.h"
GND, VCC = 558, 657


@dataclass
class Netlist:
    pullup: list[int]                        # per node: 1 = depletion pull-up (2: see notes)
    trans: list[tuple[int, int, int]]        # (gate, c1, c2)
    names: dict[int, str] = field(default_factory=dict)


def load(path: Path = NETLIST) -> Netlist:
    t = path.read_text()
    pu = [int(x) for x in re.findall(r"\d+", re.search(r"node_is_pullup\[\] = \{(.*?)\};", t, re.S).group(1))]
    body = re.search(r"transdefs\[\] = \{(.*?)\n\};", t, re.S).group(1)
    tr = [tuple(map(int, m)) for m in re.findall(r"\{(\d+), (\d+), (\d+)\}", body)]
    names = {int(v): k for k, v in re.findall(r"^\s*(\w+) = (\d+),", t, re.M)}
    names.update(load_nodenames())
    return Netlist(pu, tr, names)


NODENAMES = ROOT / "vendor" / "visual6502" / "nodenames.js"


def load_nodenames(path: Path = NODENAMES) -> dict[int, str]:
    """visual6502's node names (MIT). PLA lines are renamed 'plaN' from their comment."""
    names: dict[int, str] = {}
    if not path.exists():
        return names
    for line in path.read_text().splitlines():
        m = re.match(r'\s*"?([^":\s]+)"?\s*:\s*(\d+),?\s*(?://\s*(\S+))?', line)
        if not m:
            continue
        name, node, comment = m.group(1), int(m.group(2)), m.group(3) or ""
        if comment.startswith("pla") and comment[3:].isdigit():
            name = comment
        names.setdefault(node, name)
    return names


CP1, CP2 = 710, 943   # the two non-overlapping internal clock phases (cp1, cclk)


def breakdown(n: Netlist, an: Analysis) -> dict[str, int]:
    """Transistors (enhancement + depletion pull-ups) per functional part."""
    name = n.names.get
    parts: dict[str, int] = collections.Counter()
    pin_out = set()
    for i in an.vcc_trans:
        _, a, b = n.trans[i]
        pin_out.add(b if a == VCC else a)
    for node, net in an.gates.items():
        nm = name(node, "")
        part = "decode PLA (130 lines)" if nm.startswith("pla") else "random logic (static gates)"
        parts[part] += len(net) + 1               # + its depletion pull-up
    parts["push-pull drivers"] += len(an.vcc_trans)
    for i in an.unclassified:
        _, a, b = n.trans[i]
        parts["push-pull drivers" if (b if a == GND else a) in pin_out else "other to ground"] += 1
    for i in an.pass_trans:
        g = n.trans[i][0]
        nm = name(g, "")
        if g in (CP1, CP2):
            parts["pass: clocked latches (cp1/cp2)"] += 1
        elif nm.startswith("dpc"):
            parts["pass: register/bus switches (dpc*)"] += 1
        else:
            parts["pass: other"] += 1
    return dict(parts)


@dataclass
class Analysis:
    gates: dict[int, list[int]]              # pulled-up node -> transistor indices of its pull-down network
    pass_trans: list[int]
    vcc_trans: list[int]
    pullup_only: list[int]                   # pulled-up nodes without a pull-down network
    unclassified: list[int]


def analyse(n: Netlist) -> Analysis:
    is_gate_signal = {g for g, _, _ in n.trans}
    chan = collections.defaultdict(list)     # node -> transistors whose channel touches it
    for i, (_, a, b) in enumerate(n.trans):
        chan[a].append(i)
        chan[b].append(i)

    def internal(x: int) -> bool:
        """A node inside a series stack: no pull-up, not power, drives no gate."""
        return x not in (GND, VCC) and n.pullup[x] != 1 and x not in is_gate_signal

    vcc_trans = [i for i, (_, a, b) in enumerate(n.trans) if VCC in (a, b)]
    used: set[int] = set(vcc_trans)
    gates: dict[int, list[int]] = {}
    pullup_only = []
    for node, p in enumerate(n.pullup):
        if p != 1 or node in (GND, VCC):
            continue
        # Transistors on some path node -> ... -> GND through internal nodes only.
        net: set[int] = set()

        def reaches_gnd(x: int, via: int, seen: frozenset) -> bool:
            ok = False
            for i in chan[x]:
                if i == via or i in vcc_trans:
                    continue
                _, a, b = n.trans[i]
                y = b if a == x else a
                if y == GND:
                    net.add(i)
                    ok = True
                elif internal(y) and y not in seen and reaches_gnd(y, i, seen | {y}):
                    net.add(i)
                    ok = True
            return ok

        reaches_gnd(node, -1, frozenset({node}))
        if net:
            gates[node] = sorted(net)
            used |= net
        else:
            pullup_only.append(node)
    pass_trans = [i for i, (_, a, b) in enumerate(n.trans)
                  if i not in used and GND not in (a, b)]
    unclassified = [i for i in range(len(n.trans)) if i not in used and i not in set(pass_trans)]
    return Analysis(gates, pass_trans, vcc_trans, pullup_only, unclassified)


def two_input_equivalent(an: Analysis) -> int:
    return sum(max(1, len(t) - 1) for t in an.gates.values())


def main() -> int:
    n = load()
    an = analyse(n)
    fanin = collections.Counter(len(t) for t in an.gates.values())
    pull_gate_trans = sum(len(t) for t in an.gates.values())
    report = {
        "nodes": len(n.pullup),
        "transistors (enhancement)": len(n.trans),
        "depletion pull-ups": sum(p == 1 for p in n.pullup),
        "static gates": len(an.gates),
        "  transistors in pull-down networks": pull_gate_trans,
        "  two-input-gate equivalent": two_input_equivalent(an),
        "pass transistors": len(an.pass_trans),
        "transistors to VCC (drivers)": len(an.vcc_trans),
        "pulled-up nodes without pull-down": len(an.pullup_only),
        "unclassified transistors (touch GND, not in a gate)": len(an.unclassified),
    }
    for k, v in report.items():
        print(f"{k:55s} {v:6d}")
    print("pull-down size distribution:", dict(sorted(fanin.items())))
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "real6502_stats.json").write_text(json.dumps(
        {**report, "fanin": dict(sorted(fanin.items()))}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
