"""Median BRT corridor with signal priority, simulated in 0.2-s steps.

Buses run both ways in an exclusive median lane, served by the arterial phase. Each
intersection has either a **near-side** station (buses dwell 40 m before the stop line,
inside a typical detection zone) or a **far-side** station (after the intersection).
General traffic and side-street delays are evaluated afterwards with vertical queues
under the *realised* signal timing, so the cost of priority is measured, not assumed.

Priority strategies
-------------------
``none``       fixed-time coordination only.
``detector``   classic check-in loop at ``setback_m``: one request with
               ETA = setback / design speed; check-out loop at the stop line.
``connected``  on-board unit sends an SRM every ``srm_period_s`` with an ETA computed from
               its own state (no request while dwelling at a near-side stop), plus a cancel
               SRM when it clears the stop line. All V2X traffic goes through the
               :class:`~tsp_hub.broker.SimBroker` with latency and packet loss. The RSU also
               listens to 10-Hz BSMs and checks out implicitly once a bus is past the stop
               line, which covers lost cancel messages.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from ..broker.bus import ChannelModel, SimBroker
from ..core.config import StrictModel
from ..messages.j2735 import BSM, SRM, decode
from ..signal.controller import Decision, TSPController, TSPParams

M_PER_DEG_LAT = 111_320.0
ORIGIN = (33.7490, -84.3880)  # arbitrary georeference for BSM lat/lon


class IntersectionCfg(StrictModel):
    x_m: float
    offset_s: float = 0.0
    station: Literal["near", "far", "none"] = "far"


class CorridorConfig(StrictModel):
    length_m: float = 1800.0
    intersections: list[IntersectionCfg]
    signal: TSPParams = Field(default_factory=TSPParams)
    bus_headway_s: float = 300.0
    cruise_mean_mps: float = 13.0
    cruise_sd_mps: float = 1.2
    accel_mps2: float = 1.0
    decel_mps2: float = 1.3
    dwell_mean_s: float = 20.0
    dwell_sd_s: float = 8.0
    near_station_m: float = 40.0
    far_station_m: float = 35.0
    duration_s: float = 7200.0
    warmup_s: float = 600.0
    dt_s: float = 0.2
    side_vph: float = 450.0
    arterial_vph: float = 1100.0
    sat_vphpl: float = 1800.0
    arterial_lanes: int = 2


class Strategy(StrictModel):
    kind: Literal["none", "detector", "connected"]
    setback_m: float = 150.0
    design_speed_mps: float = 13.0
    srm_period_s: float = 1.0
    request_horizon_m: float = 300.0
    loss: float = Field(0.0, ge=0.0, lt=1.0)
    latency_s: float = 0.05


@dataclass
class _Stop:
    s: float  # position along the bus's own direction
    kind: str  # "station" | "signal"
    k: int  # intersection index
    served: bool = False


@dataclass
class _Bus:
    bid: int
    direction: int  # +1 EB, -1 WB
    t_enter: float
    v_cruise: float
    stops: list[_Stop]
    s: float = 0.0
    v: float = 0.0
    dwell_left: float = 0.0
    dwell_total: float = 0.0
    msg_count: int = 0
    passed: dict[int, float] = field(default_factory=dict)
    detector_fired: set[int] = field(default_factory=set)
    signal_stop_s: float = 0.0
    signal_stops: int = 0
    stopped_flag: bool = False
    t_exit: float | None = None


@dataclass(frozen=True)
class RunResult:
    buses: pd.DataFrame
    priority: pd.DataFrame
    traffic: pd.DataFrame
    messages: dict[str, int]


def _time_to_cover(d: float, v: float, a: float, vmax: float) -> float:
    if d <= 0:
        return 0.0
    t_acc = max(vmax - v, 0.0) / a
    d_acc = v * t_acc + 0.5 * a * t_acc * t_acc
    if d_acc >= d:
        return (-v + math.sqrt(v * v + 2 * a * d)) / a
    return t_acc + (d - d_acc) / vmax


class CorridorSim:
    def __init__(self, cfg: CorridorConfig, strategy: Strategy, seed: int) -> None:
        self.cfg, self.st = cfg, strategy
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        enabled = strategy.kind != "none"
        self.ctrl = [TSPController(cfg.signal.model_copy(update={"offset_s": ic.offset_s, "enabled": enabled}))
                     for ic in cfg.intersections]
        self.broker = SimBroker(ChannelModel(latency_s=strategy.latency_s, jitter_s=0.01, loss=strategy.loss), seed=seed + 7)
        for k in range(len(cfg.intersections)):
            self.broker.subscribe(f"srm/{k}", self._on_srm)
            self.broker.subscribe(f"bsm/{k}", self._on_bsm)
        self.buses: list[_Bus] = []
        self.done: list[_Bus] = []
        self.requests: list[dict[str, object]] = []

    # -------------------------------------------------------------- geometry
    def _stops(self, direction: int) -> list[_Stop]:
        c = self.cfg
        out: list[_Stop] = []
        for k, ic in enumerate(c.intersections):
            line = ic.x_m if direction > 0 else c.length_m - ic.x_m
            if ic.station == "near":
                out.append(_Stop(line - c.near_station_m, "station", k))
            out.append(_Stop(line, "signal", k))
            if ic.station == "far":
                out.append(_Stop(line + c.far_station_m, "station", k))
        return sorted(out, key=lambda s: s.s)

    def _line(self, bus: _Bus, k: int) -> float:
        x = self.cfg.intersections[k].x_m
        return x if bus.direction > 0 else self.cfg.length_m - x

    def _rid(self, bus: _Bus, k: int) -> int:
        return bus.bid * 10 + k

    # ------------------------------------------------------------- messaging
    def _on_srm(self, topic: str, frame: bytes, t_arr: float) -> None:
        msg = decode(frame)
        assert isinstance(msg, SRM)
        ctrl = self.ctrl[msg.intersection_id]
        if msg.cancel:
            ctrl.check_out(t_arr, msg.request_id)
            return
        d = ctrl.request(t_arr, t_arr + msg.eta_s, msg.request_id)
        self.requests.append({"t": t_arr, "request": msg.request_id, "k": msg.intersection_id, "decision": d.value,
                              "source": "srm"})

    def _on_bsm(self, topic: str, frame: bytes, t_arr: float) -> None:
        msg = decode(frame)
        assert isinstance(msg, BSM)
        k = int(topic.split("/")[1])
        x = (msg.lon_deg - ORIGIN[1]) * M_PER_DEG_LAT * math.cos(math.radians(ORIGIN[0]))
        heading_east = abs(msg.heading_deg - 90.0) < 45.0
        line = self.cfg.intersections[k].x_m
        if (heading_east and x > line + 2.0) or (not heading_east and x < line - 2.0):
            self.ctrl[k].check_out(t_arr, msg.temp_id * 10 + k)  # implicit check-out from position

    def _publish_bsm(self, bus: _Bus, t: float) -> None:
        x = bus.s if bus.direction > 0 else self.cfg.length_m - bus.s
        bus.msg_count = (bus.msg_count + 1) % 128
        lon = ORIGIN[1] + x / (M_PER_DEG_LAT * math.cos(math.radians(ORIGIN[0])))
        bsm = BSM(bus.msg_count, bus.bid, int((t * 1000) % 60000), ORIGIN[0], lon, max(bus.v, 0.0),
                  90.0 if bus.direction > 0 else 270.0, 0.0, 18.3, 2.6, vehicle_role=16)
        nxt = next((s for s in bus.stops if s.kind == "signal" and s.s >= bus.s - 30.0), None)
        if nxt is not None:
            self.broker.publish(f"bsm/{nxt.k}", bsm.pack(), t)

    # ------------------------------------------------------------- priority
    def _priority(self, bus: _Bus, t: float, step_index: int) -> None:
        st = self.st
        if st.kind == "none":
            return
        for stop in bus.stops:
            if stop.kind != "signal" or stop.k in bus.passed:
                continue
            d = stop.s - bus.s
            if d < 0:
                continue
            if st.kind == "detector":
                if stop.k not in bus.detector_fired and d <= st.setback_m:
                    bus.detector_fired.add(stop.k)
                    rid = self._rid(bus, stop.k)
                    dec = self.ctrl[stop.k].request(t, t + d / st.design_speed_mps, rid)
                    self.requests.append({"t": t, "request": rid, "k": stop.k, "decision": dec.value, "source": "loop"})
            elif st.kind == "connected" and d <= st.request_horizon_m:
                pending_near = any(s.kind == "station" and s.k == stop.k and not s.served and s.s < stop.s for s in bus.stops)
                if pending_near or bus.dwell_left > 0:
                    break  # door still open / stop ahead: ETA unknown, do not request yet
                period = max(1, int(round(st.srm_period_s / self.cfg.dt_s)))
                if (step_index + bus.bid) % period == 0:
                    eta = _time_to_cover(d, bus.v, self.cfg.accel_mps2, bus.v_cruise)
                    srm = SRM(self._rid(bus, stop.k), bus.bid, stop.k, 2 if bus.direction > 0 else 6, eta, 2.0)
                    self.broker.publish(f"srm/{stop.k}", srm.pack(), t)
            break  # only the next signal

    # -------------------------------------------------------------- dynamics
    def _step_bus(self, bus: _Bus, t: float, dt: float) -> None:
        c = self.cfg
        if bus.dwell_left > 0:
            bus.dwell_left -= dt
            if bus.dwell_left <= 0:
                for s in bus.stops:
                    if s.kind == "station" and not s.served and abs(s.s - bus.s) < 1.0:
                        s.served = True
                        break
            return
        target: float | None = None
        for s in bus.stops:
            if s.s < bus.s - 0.5:
                continue
            if s.kind == "station" and not s.served:
                target = s.s
                break
            if s.kind == "signal" and s.k not in bus.passed:
                state = self.ctrl[s.k].state(t)
                d = s.s - bus.s
                can_stop = bus.v * bus.v / (2 * 3.0) <= d + 0.5
                if state == "R" or (state == "Y" and can_stop):
                    target = s.s - 1.0
                    break
        acc = c.accel_mps2 if bus.v < bus.v_cruise else 0.0
        if target is not None:
            d = target - bus.s
            if d <= 0.3 and bus.v < 0.5:
                bus.v = 0.0
                acc = 0.0
                station = next((s for s in bus.stops if s.kind == "station" and not s.served and abs(s.s - bus.s) < 1.5), None)
                if station is not None:
                    bus.dwell_left = max(5.0, self.rng.normal(c.dwell_mean_s, c.dwell_sd_s))
                    bus.dwell_total += bus.dwell_left
                    return
            else:
                need = bus.v * bus.v / (2.0 * max(d, 0.1))
                if need >= 0.7 * c.decel_mps2:
                    acc = -min(need, 4.0)
        v_new = max(0.0, bus.v + acc * dt)
        ds = 0.5 * (bus.v + v_new) * dt
        if target is not None:
            ds = min(ds, max(target - bus.s, 0.0))
        old = bus.s
        bus.s += ds
        bus.v = v_new
        slow = bus.v < 1.0 and bus.dwell_left <= 0
        near_signal = any(s.kind == "signal" and s.k not in bus.passed and 0 <= s.s - bus.s < 80 for s in bus.stops)
        if slow and near_signal:
            bus.signal_stop_s += dt
            if not bus.stopped_flag:
                bus.signal_stops += 1
                bus.stopped_flag = True
        elif bus.v > 3.0:
            bus.stopped_flag = False
        for s in bus.stops:
            if s.kind == "signal" and s.k not in bus.passed and old < s.s <= bus.s:
                bus.passed[s.k] = t + dt
                rid = self._rid(bus, s.k)
                if self.st.kind == "detector":
                    self.ctrl[s.k].check_out(t + dt, rid)
                elif self.st.kind == "connected":
                    cancel = SRM(rid, bus.bid, s.k, 2 if bus.direction > 0 else 6, 0.0, 0.0, cancel=True)
                    self.broker.publish(f"srm/{s.k}", cancel.pack(), t + dt)

    # ------------------------------------------------------------------- run
    def run(self) -> RunResult:
        c = self.cfg
        dt = c.dt_s
        n = int(round(c.duration_s / dt))
        next_dep = {1: float(self.rng.uniform(0, c.bus_headway_s)), -1: float(self.rng.uniform(0, c.bus_headway_s))}
        bid = 1
        bsm_every = max(1, int(round(0.1 / dt)))
        for i in range(n):
            t = i * dt
            for d in (1, -1):
                if t >= next_dep[d]:
                    v = float(np.clip(self.rng.normal(c.cruise_mean_mps, c.cruise_sd_mps), 9.0, 16.0))
                    self.buses.append(_Bus(bid, d, t, v, self._stops(d), v=v))
                    bid += 1
                    next_dep[d] += c.bus_headway_s * float(self.rng.uniform(0.85, 1.15))
            self.broker.advance(t)
            for bus in self.buses:
                self._priority(bus, t, i)
                self._step_bus(bus, t, dt)
                if self.st.kind == "connected" and i % bsm_every == 0:
                    self._publish_bsm(bus, t)
            still = []
            for bus in self.buses:
                if bus.s >= c.length_m:
                    bus.t_exit = t + dt
                    self.done.append(bus)
                else:
                    still.append(bus)
            self.buses = still
        return self._results()

    # --------------------------------------------------------------- metrics
    def _results(self) -> RunResult:
        c = self.cfg
        rows = []
        for b in self.done:
            if b.t_enter < c.warmup_s:
                continue
            rows.append({"bus": b.bid, "direction": "EB" if b.direction > 0 else "WB", "t_enter": b.t_enter,
                         "travel_time_s": b.t_exit - b.t_enter, "dwell_s": b.dwell_total,
                         "signal_delay_s": b.signal_stop_s, "signal_stops": b.signal_stops})
        buses = pd.DataFrame(rows)
        passes = {(self._rid(b, k)): tp for b in self.done for k, tp in b.passed.items()}
        prow = []
        for k, ctrl in enumerate(self.ctrl):
            for cyc, cobj in ctrl.cycles.items():
                if cobj.owner is None:
                    continue
                used_ext, early = ctrl.priority_seconds(cyc)
                p = ctrl.params
                base_end = p.offset_s + cyc * p.cycle_s + p.arterial_green_s
                base_next = p.offset_s + (cyc + 1) * p.cycle_s
                tp = passes.get(cobj.owner)
                if used_ext > 0:
                    useful = tp is not None and base_end < tp <= base_end + used_ext + 0.5
                    prow.append({"k": k, "cycle": cyc, "type": "extension", "seconds": used_ext, "useful": useful})
                if early > 0:
                    useful = tp is not None and base_next - early - 0.5 <= tp < base_next
                    prow.append({"k": k, "cycle": cyc, "type": "early_green", "seconds": early, "useful": useful})
        traffic = self._traffic_delays()
        return RunResult(buses, pd.DataFrame(prow, columns=["k", "cycle", "type", "seconds", "useful"]), traffic,
                         {"published": self.broker.published, "dropped": self.broker.dropped,
                          "delivered": self.broker.delivered})

    def _traffic_delays(self) -> pd.DataFrame:
        """Vertical-queue delay of side-street and arterial general traffic under realised timing."""
        c = self.cfg
        rng = np.random.default_rng(self.seed + 99)
        rows = []
        for k, ctrl in enumerate(self.ctrl):
            last_cycle = ctrl.cycle_of(c.duration_s) + 1
            art = [ctrl.arterial_window(j) for j in range(-1, last_cycle + 1)]
            side = [ctrl.side_green_window(j) for j in range(-1, last_cycle + 1)]
            for name, windows, vph, lanes in (("side", side, c.side_vph, 1), ("arterial", art, c.arterial_vph, c.arterial_lanes)):
                for approach in range(2):
                    h = 3600.0 / (c.sat_vphpl * lanes)
                    arr = np.cumsum(rng.exponential(3600.0 / vph, int(vph * c.duration_s / 3600 * 1.3) + 10))
                    arr = arr[arr < c.duration_s]
                    starts = np.array([w[0] for w in windows]) + 2.0  # start-up lost time
                    ends = np.array([w[1] for w in windows]) + 2.0  # 2 s of yellow used
                    last = -1e9
                    delays = []
                    for a in arr:
                        tdep = max(a, last + h)
                        j = int(np.searchsorted(ends, tdep, side="right"))
                        while j < len(starts) and not (starts[j] <= tdep < ends[j]):
                            if tdep < starts[j]:
                                tdep = starts[j]
                            else:
                                j += 1
                        last = tdep
                        if a >= c.warmup_s:
                            delays.append(tdep - a)
                    rows.append({"k": k, "movement": name, "approach": approach, "mean_delay_s": float(np.mean(delays)),
                                 "veh": len(delays)})
        return pd.DataFrame(rows)
