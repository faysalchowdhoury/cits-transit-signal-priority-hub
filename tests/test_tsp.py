from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsp_hub.api.app import create_app
from tsp_hub.broker.bus import AsyncBroker, ChannelModel, SimBroker
from tsp_hub.cli import bench_broker
from tsp_hub.core.config import load_model
from tsp_hub.core.exceptions import BrokerError, MessageError
from tsp_hub.messages.j2735 import BSM, SRM, SSM, decode
from tsp_hub.signal.controller import Decision, TSPController, TSPParams
from tsp_hub.sim.corridor import CorridorConfig, CorridorSim, Strategy

ROOT = Path(__file__).resolve().parents[1]


def test_message_roundtrip_and_units() -> None:
    b = BSM(5, 123456, 59999, 33.7490123, -84.3880456, 13.37, 271.3, -1.25, 18.3, 2.6, 16)
    d = decode(b.pack())
    assert isinstance(d, BSM)
    assert d.lat_deg == pytest.approx(b.lat_deg, abs=1e-7) and d.lon_deg == pytest.approx(b.lon_deg, abs=1e-7)
    assert d.speed_mps == pytest.approx(13.36, abs=0.02)  # 0.02 m/s resolution
    assert d.heading_deg == pytest.approx(271.3, abs=0.0125)
    s = SRM(77, 9, 2, 6, 12.34, 2.0, cancel=True)
    assert decode(s.pack()) == SRM(77, 9, 2, 6, 12.3, 2.0, cancel=True)
    assert decode(SSM(77, 2, 1).pack()) == SSM(77, 2, 1)
    with pytest.raises(MessageError):
        BSM(200, 1, 0, 0, 0, 1, 0, 0, 1, 1)
    with pytest.raises(MessageError):
        decode(b"\x99abc")
    with pytest.raises(MessageError):
        decode(b"\x14\x00")


def test_sim_broker_latency_loss_and_order() -> None:
    got: list[tuple[float, bytes]] = []
    b = SimBroker(ChannelModel(latency_s=0.1, jitter_s=0.0, loss=0.0))
    b.subscribe("a", lambda topic, f, t: got.append((t, f)))
    b.publish("a", b"1", 0.0)
    b.publish("a", b"2", 0.05)
    b.advance(0.12)
    assert [f for _, f in got] == [b"1"]
    b.advance(1.0)
    assert [f for _, f in got] == [b"1", b"2"] and got[0][0] == pytest.approx(0.1)
    lossy = SimBroker(ChannelModel(latency_s=0.0, jitter_s=0.0, loss=0.3), seed=1)
    lossy.subscribe("a", lambda *_: None)
    for i in range(5000):
        lossy.publish("a", b"x", i * 0.01)
    lossy.advance(1e9)
    assert lossy.dropped / lossy.published == pytest.approx(0.3, abs=0.03)
    with pytest.raises(BrokerError):
        ChannelModel(loss=1.0)


def test_async_broker_drop_oldest_and_benchmark() -> None:
    async def scenario() -> None:
        b = AsyncBroker(queue_size=2)
        q = b.subscribe("t")
        for i in range(5):
            b.publish("t", bytes([i]))
        assert q.qsize() == 2 and b.dropped_overflow == 3
        assert (await q.get())[1] == bytes([3])
        r = await bench_broker(2000, 2)
        assert r["deliveries_per_s"] > 1000
        b.close()
        with pytest.raises(BrokerError):
            b.publish("t", b"x")

    asyncio.run(scenario())


P = TSPParams()  # C=100, arterial green 0-52, Y 52-56, AR 56-58, side green 58-94, side Y/AR 94-100


def test_controller_nominal_states() -> None:
    c = TSPController(P)
    assert c.state(10) == "G" and c.state(53) == "Y" and c.state(57) == "R" and c.state(80) == "R" and c.state(100.5) == "G"
    assert c.side_green_window(0) == pytest.approx((58.0, 94.0))


def test_green_extension_and_checkout() -> None:
    c = TSPController(P)
    assert c.request(45.0, 57.0, 1) == Decision.EXTEND
    assert c.state(56.0) == "G" and c.state(57.5) == "G"
    assert c.side_green_window(0)[0] == pytest.approx(58.0 + 6.0)  # side street lost the 6 s
    c.check_out(57.2, 1)
    assert c.state(57.3) == "Y"  # extension ended at check-out
    assert c.priority_seconds(0)[0] == pytest.approx(5.2)
    assert c.request(46.0, 58.0, 2) == Decision.REJECTED  # lockout: one action per cycle


def test_early_green_respects_side_minimum() -> None:
    c = TSPController(P)
    # request during side green, bus arrives at t = 80 (next arterial green would start at 100)
    assert c.request(70.0, 80.0, 3) == Decision.EARLY
    s_next, _ = c.arterial_window(1)
    assert s_next == pytest.approx(90.0)  # max early green 10 s
    side_s, side_e = c.side_green_window(0)
    assert side_e - side_s >= P.side_min_green_s - 1e-9
    c2 = TSPController(P)
    assert c2.request(59.0, 60.0, 4) == Decision.EARLY
    s2, _ = c2.arterial_window(1)
    assert s2 >= 58.0 + P.side_min_green_s + 6.0 - 1e-9  # cannot cut side green below 15 s


def test_not_needed_and_disabled() -> None:
    assert TSPController(P).request(10.0, 30.0, 5) == Decision.NOT_NEEDED
    assert TSPController(P.model_copy(update={"enabled": False})).request(45.0, 57.0, 6) == Decision.REJECTED
    with pytest.raises(ValueError):
        TSPParams(arterial_green_s=80)


def test_corridor_tsp_reduces_bus_signal_delay() -> None:
    cfg = load_model(ROOT / "configs" / "corridor.yaml", CorridorConfig).model_copy(update={"duration_s": 3000.0, "warmup_s": 300.0})
    base = CorridorSim(cfg, Strategy(kind="none"), 3).run()
    cv = CorridorSim(cfg, Strategy(kind="connected"), 3).run()
    assert len(base.buses) == len(cv.buses) > 10
    assert cv.buses["signal_delay_s"].mean() < base.buses["signal_delay_s"].mean()
    assert len(cv.priority) > 0 and base.priority.empty
    assert cv.messages["published"] > 0 and cv.messages["dropped"] == 0
    assert set(base.traffic["movement"]) == {"side", "arterial"}


def test_api_ingest() -> None:
    with TestClient(create_app()) as c:
        frame = SRM(10, 1, 0, 2, 5.0, 1.0).pack()
        assert c.post("/v1/v2x", content=frame).json() == {"accepted": "SRM"}
        assert c.post("/v1/v2x", content=b"\x00").status_code == 422
        assert c.get("/v1/rsu/0").json()["arterial"] in ("G", "Y", "R")
        assert c.get("/v1/rsu/9").status_code == 404
        assert c.get("/health").json()["published"] == 1
