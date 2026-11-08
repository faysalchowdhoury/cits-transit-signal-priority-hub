"""Live RSU service.

``POST /v1/v2x``        raw J2735-shaped frame (application/octet-stream) -> broker
``GET  /v1/rsu/{k}``    current indication and priority log of intersection ``k``
``GET  /health``        broker counters

Messages are decoded and dispatched by an asyncio consumer task per topic, so the HTTP
handler only validates and enqueues (non-blocking publish, drop-oldest back-pressure).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .. import __version__
from ..broker.bus import AsyncBroker
from ..core.exceptions import MessageError
from ..messages.j2735 import BSM, SRM, decode
from ..signal.controller import TSPController, TSPParams


def create_app(n_intersections: int = 4, params: TSPParams | None = None) -> FastAPI:
    params = params or TSPParams()
    ctrls = [TSPController(params.model_copy(update={"offset_s": 0.0})) for _ in range(n_intersections)]
    broker = AsyncBroker(queue_size=4096)
    t0 = time.monotonic()
    stats = {"srm": 0, "bsm": 0, "decisions": 0}

    async def consume(q: asyncio.Queue[tuple[float, bytes]]) -> None:
        while True:
            _, frame = await q.get()
            msg = decode(frame)
            now = time.monotonic() - t0
            if isinstance(msg, SRM) and 0 <= msg.intersection_id < len(ctrls):
                stats["srm"] += 1
                c = ctrls[msg.intersection_id]
                if msg.cancel:
                    c.check_out(now, msg.request_id)
                else:
                    c.request(now, now + msg.eta_s, msg.request_id)
                    stats["decisions"] += 1
            elif isinstance(msg, BSM):
                stats["bsm"] += 1

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(consume(broker.subscribe("v2x")))]
        yield
        broker.close()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    app = FastAPI(title="TSP RSU hub", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "published": broker.published, "overflow_drops": broker.dropped_overflow, **stats}

    @app.post("/v1/v2x", status_code=202)
    async def ingest(request: Request) -> dict[str, str]:
        frame = await request.body()
        try:
            msg = decode(frame)
        except MessageError as exc:
            raise HTTPException(422, str(exc)) from exc
        broker.publish("v2x", frame)
        return {"accepted": type(msg).__name__}

    @app.get("/v1/rsu/{k}")
    async def rsu(k: int) -> dict[str, object]:
        if not 0 <= k < len(ctrls):
            raise HTTPException(404, "unknown intersection")
        now = time.monotonic() - t0
        return {"t": round(now, 2), "arterial": ctrls[k].state(now), "log": ctrls[k].log[-20:]}

    return app
