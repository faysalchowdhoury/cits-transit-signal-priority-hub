"""``tsp-hub`` command line: ``study``, ``simulate``, ``bench-broker``, ``serve``."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np

from .core.logging import configure_logging


async def bench_broker(n: int, subscribers: int) -> dict[str, float]:
    from .broker.bus import AsyncBroker
    from .messages.j2735 import BSM, decode

    b = AsyncBroker(queue_size=n + 10)
    qs = [b.subscribe("bsm") for _ in range(subscribers)]
    frame = BSM(1, 42, 1000, 33.749, -84.388, 13.0, 90.0, 0.0, 18.3, 2.6, 16).pack()
    lat: list[float] = []

    async def consumer(q: asyncio.Queue[tuple[float, bytes]]) -> None:
        for _ in range(n):
            stamp, f = await q.get()
            decode(f)
            lat.append(time.perf_counter() - stamp)

    tasks = [asyncio.create_task(consumer(q)) for q in qs]
    t0 = time.perf_counter()
    for i in range(n):
        b.publish("bsm", frame)
        if i % 256 == 0:
            await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - t0
    a = np.array(lat) * 1e3
    return {"messages": n, "subscribers": subscribers, "deliveries_per_s": n * subscribers / wall,
            "p50_ms": float(np.percentile(a, 50)), "p99_ms": float(np.percentile(a, 99))}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tsp-hub")
    p.add_argument("--log-level", default="WARNING")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("study")
    s.add_argument("--config", default="configs/corridor.yaml")
    s.add_argument("--out", default="results/brt_corridor")
    s.add_argument("--seeds", type=int, nargs="+", default=[11, 23, 37, 41, 59, 67, 73, 89, 97, 101])
    s.add_argument("--workers", type=int, default=2)
    s = sub.add_parser("simulate")
    s.add_argument("--config", default="configs/corridor.yaml")
    s.add_argument("--kind", choices=["none", "detector", "connected"], default="connected")
    s.add_argument("--setback", type=float, default=150.0)
    s.add_argument("--loss", type=float, default=0.0)
    s.add_argument("--seed", type=int, default=1)
    s = sub.add_parser("bench-broker")
    s.add_argument("--messages", type=int, default=200_000)
    s.add_argument("--subscribers", type=int, default=4)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)
    configure_logging(a.log_level)
    if a.cmd == "study":
        from .services.report import write_report
        from .services.study import run_study

        summary = run_study(a.config, a.out, a.seeds, a.workers)
        print(write_report(summary, Path(a.out)).read_text(encoding="utf-8"))
    elif a.cmd == "simulate":
        from .core.config import load_model
        from .sim.corridor import CorridorConfig, CorridorSim, Strategy

        cfg = load_model(a.config, CorridorConfig)
        r = CorridorSim(cfg, Strategy(kind=a.kind, setback_m=a.setback, loss=a.loss), a.seed).run()
        print(r.buses.describe().to_string())
        print(r.priority.groupby("type")["useful"].agg(["count", "mean"]).to_string() if len(r.priority) else "no priority")
        print(json.dumps(r.messages))
    elif a.cmd == "bench-broker":
        print(json.dumps(asyncio.run(bench_broker(a.messages, a.subscribers)), indent=2))
    else:
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
