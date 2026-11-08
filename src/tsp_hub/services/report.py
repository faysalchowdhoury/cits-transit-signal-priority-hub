"""Markdown report for the TSP study."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

LABELS = {
    "no_tsp": "No TSP", "loop_75m": "Loop, 75 m setback", "loop_150m": "Loop, 150 m", "loop_250m": "Loop, 250 m",
    "loop_350m": "Loop, 350 m", "cv_loss0": "Connected, 0% loss", "cv_loss30": "Connected, 30% loss",
    "cv_loss90": "Connected, 90% loss", "cv_loss97": "Connected, 97% loss",
}


def _f(m: dict[str, float], fmt: str = "{:.1f}", pct: bool = False) -> str:
    s = fmt.format(m["mean"]) + " ± " + fmt.format(m["ci"])
    if pct and "pct_vs_no_tsp" in m:
        p = m["p_paired"]
        s += f" ({m['pct_vs_no_tsp']:+.0f}%{'' if p < 0.05 else ', n.s.'})"
    return s


def write_report(summary: dict[str, Any], out_dir: Path) -> Path:
    L = ["# TSP strategy study", "",
         f"Seeds {', '.join(map(str, summary['seeds']))}; 2 h per run after a 10-min warm-up; paired t-tests against No TSP "
         "(same seeds = same buses, dwells and traffic). n.s. = not significant at 5%.", "",
         "| Strategy | Bus signal delay / trip (s) | Bus travel time (s) | Travel-time SD (s) | Signal stops / trip | "
         "Priority actions / run | Used by the bus | Side-street delay (s/veh) | Arterial GP delay (s/veh) |",
         "|---|---|---|---|---|---|---|---|---|"]
    for k, lab in LABELS.items():
        e = summary["scenarios"][k]
        used = e["useful_share"]["mean"]
        L.append(f"| {lab} | {_f(e['bus_signal_delay_s'], pct=True)} | {_f(e['bus_travel_time_s'], pct=True)} | "
                 f"{_f(e['bus_travel_time_sd_s'])} | {e['bus_signal_stops']['mean']:.2f} | {e['priority_actions']['mean']:.0f} | "
                 f"{'-' if math.isnan(used) else f'{100 * used:.0f}%'} | {_f(e['side_delay_s'], pct=True)} | "
                 f"{e['arterial_delay_s']['mean']:.1f} |")
    L += ["", "![Strategies](figures/tsp_strategies.png)", ""]
    md = out_dir / "RESULTS.md"
    md.write_text("\n".join(L), encoding="utf-8")
    return md
