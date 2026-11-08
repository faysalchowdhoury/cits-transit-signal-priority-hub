"""TSP strategy study: no TSP vs. detector setbacks vs. connected TSP under packet loss."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..core.config import load_model
from ..sim.corridor import CorridorConfig, CorridorSim, Strategy

SCENARIOS: dict[str, dict[str, Any]] = {
    "no_tsp": {"kind": "none"},
    "loop_75m": {"kind": "detector", "setback_m": 75},
    "loop_150m": {"kind": "detector", "setback_m": 150},
    "loop_250m": {"kind": "detector", "setback_m": 250},
    "loop_350m": {"kind": "detector", "setback_m": 350},
    "cv_loss0": {"kind": "connected", "loss": 0.0},
    "cv_loss30": {"kind": "connected", "loss": 0.30},
    "cv_loss90": {"kind": "connected", "loss": 0.90},
    "cv_loss97": {"kind": "connected", "loss": 0.97},
}


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = CorridorConfig.model_validate(payload["cfg"])
    res = CorridorSim(cfg, Strategy.model_validate(payload["strategy"]), payload["seed"]).run()
    b, p, tr = res.buses, res.priority, res.traffic
    out = {
        "scenario": payload["scenario"], "seed": payload["seed"], "buses": len(b),
        "bus_travel_time_s": float(b["travel_time_s"].mean()),
        "bus_travel_time_sd_s": float(b["travel_time_s"].std(ddof=1)),
        "bus_travel_time_p95_s": float(np.percentile(b["travel_time_s"], 95)),
        "bus_signal_delay_s": float(b["signal_delay_s"].mean()),
        "bus_signal_stops": float(b["signal_stops"].mean()),
        "priority_actions": int(len(p)),
        "priority_seconds": float(p["seconds"].sum()) if len(p) else 0.0,
        "useful_share": float(p["useful"].mean()) if len(p) else math.nan,
        "extensions": int((p["type"] == "extension").sum()) if len(p) else 0,
        "early_greens": int((p["type"] == "early_green").sum()) if len(p) else 0,
        "side_delay_s": float(tr.loc[tr["movement"] == "side", "mean_delay_s"].mean()),
        "arterial_delay_s": float(tr.loc[tr["movement"] == "arterial", "mean_delay_s"].mean()),
        **{f"msg_{k}": v for k, v in res.messages.items()},
    }
    return out


def run_study(config: str | Path, out_dir: str | Path, seeds: list[int], workers: int = 2) -> dict[str, Any]:
    cfg = load_model(config, CorridorConfig)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payloads = [{"cfg": cfg.model_dump(mode="json"), "strategy": st, "scenario": name, "seed": s}
                for name, st in SCENARIOS.items() for s in seeds]
    if workers <= 1:
        rows = [_run(p) for p in payloads]
    else:
        with ProcessPoolExecutor(min(workers, os.cpu_count() or 1)) as pool:
            rows = list(pool.map(_run, payloads))
    df = pd.DataFrame(rows)
    df.to_csv(out / "per_seed_metrics.csv", index=False)
    metrics = ["bus_travel_time_s", "bus_travel_time_sd_s", "bus_travel_time_p95_s", "bus_signal_delay_s",
               "bus_signal_stops", "priority_actions", "useful_share", "side_delay_s", "arterial_delay_s", "msg_dropped"]
    summary: dict[str, Any] = {"seeds": seeds, "scenarios": {}}
    base = df[df["scenario"] == "no_tsp"].sort_values("seed")
    for name in SCENARIOS:
        sub = df[df["scenario"] == name].sort_values("seed")
        entry: dict[str, Any] = {}
        for m in metrics:
            x = sub[m].to_numpy(float)
            ci = float(stats.t.ppf(0.975, len(x) - 1) * np.nanstd(x, ddof=1) / math.sqrt(len(x))) if len(x) > 1 else math.nan
            entry[m] = {"mean": float(np.nanmean(x)), "ci": ci}
            if name != "no_tsp" and m in ("bus_signal_delay_s", "bus_travel_time_s", "side_delay_s", "bus_travel_time_sd_s"):
                a, bb = base[m].to_numpy(float), x
                entry[m]["pct_vs_no_tsp"] = 100.0 * (bb.mean() - a.mean()) / a.mean()
                entry[m]["p_paired"] = float(stats.ttest_rel(a, bb).pvalue)
        summary["scenarios"][name] = entry
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    _plot(df, out / "figures")
    return summary


def _plot(df: pd.DataFrame, fig_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig_dir.mkdir(parents=True, exist_ok=True)
    g = df.groupby("scenario").mean(numeric_only=True).reindex(list(SCENARIOS))
    fig, axes = plt.subplots(1, 3, figsize=(14, 3.8))
    colors = ["#868e96"] + ["#f59f00"] * 4 + ["#1c7ed6"] * 4
    axes[0].bar(g.index, g["bus_signal_delay_s"], color=colors)
    axes[0].set_ylabel("bus signal delay per trip (s)")
    axes[1].bar(g.index, 100 * g["useful_share"].fillna(0), color=colors)
    axes[1].set_ylabel("priority actions the bus used (%)")
    axes[2].bar(g.index, g["side_delay_s"], color=colors)
    axes[2].set_ylabel("side-street delay (s/veh)")
    axes[2].set_ylim(g["side_delay_s"].min() * 0.9, g["side_delay_s"].max() * 1.05)
    for ax in axes:
        ax.tick_params(axis="x", rotation=45)
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "tsp_strategies.png", dpi=130)
    plt.close(fig)
