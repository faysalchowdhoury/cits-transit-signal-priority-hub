"""Two-phase coordinated controller with in-cycle transit signal priority.

Cycle ``k`` (local time from ``offset + k C``)::

    arterial green  [S_k, E_k)      S_k = k C - early_{k-1},  E_k = k C + G_a + ext_k
    arterial Y + AR [E_k, E_k + Y_a + R_a)
    side green      [E_k + Y_a + R_a, (k+1) C - early_k - Y_s - R_s)
    side Y + AR     ... until (k+1) C - early_k = S_{k+1}

The BRT median lanes run with the arterial phase (phi2/phi6). Priority time is always
taken from the side-street green **inside the same cycle**, so the cycle length and offset
never change and coordination needs no recovery period:

* **green extension** ``ext_k <= max_extension``: hold the arterial green until the bus arrives;
* **early green** ``early_k <= max_early_green``: truncate the side green so the next
  arterial green starts sooner;
* both keep the side street at or above ``side_min_green`` (pedestrian clearance);
* **lockout**: at most one priority action per cycle;
* **check-out**: when the bus clears the stop line, an extension still running is ended
  immediately, so unused priority time returns to the side street.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from pydantic import Field, model_validator

from ..core.config import StrictModel


class Decision(str, Enum):
    EXTEND = "extension"
    EARLY = "early_green"
    NOT_NEEDED = "not_needed"  # bus arrives on green anyway
    REJECTED = "rejected"  # outside reach, side minimum, or locked out


class TSPParams(StrictModel):
    cycle_s: float = Field(100.0, gt=20.0)
    offset_s: float = Field(0.0, ge=0.0)
    arterial_green_s: float = Field(52.0, gt=5.0)
    arterial_yellow_s: float = Field(4.0, ge=3.0, le=6.0)
    arterial_red_s: float = Field(2.0, ge=0.0)
    side_yellow_s: float = Field(4.0, ge=3.0, le=6.0)
    side_red_s: float = Field(2.0, ge=0.0)
    side_min_green_s: float = Field(15.0, gt=0.0)
    max_extension_s: float = Field(10.0, ge=0.0)
    max_early_green_s: float = Field(10.0, ge=0.0)
    arrival_buffer_s: float = Field(1.0, ge=0.0, description="extra green after the predicted arrival")
    enabled: bool = True

    @property
    def side_green_s(self) -> float:
        return self.cycle_s - self.arterial_green_s - self.arterial_yellow_s - self.arterial_red_s - self.side_yellow_s - self.side_red_s

    @model_validator(mode="after")
    def _check(self) -> "TSPParams":
        if self.side_green_s < self.side_min_green_s:
            raise ValueError(f"side green {self.side_green_s:.1f}s is below its minimum {self.side_min_green_s}s")
        return self


@dataclass
class _Cycle:
    ext: float = 0.0
    early: float = 0.0
    locked: bool = False
    owner: int | None = None  # request id that owns this cycle's action
    checked_out_at: float | None = None


@dataclass
class TSPController:
    params: TSPParams
    cycles: dict[int, _Cycle] = field(default_factory=dict)
    log: list[dict[str, object]] = field(default_factory=list)

    # ---------------------------------------------------------------- timing
    def _c(self, k: int) -> _Cycle:
        return self.cycles.setdefault(k, _Cycle())

    def cycle_of(self, t: float) -> int:
        return math.floor((t - self.params.offset_s) / self.params.cycle_s)

    def arterial_window(self, k: int) -> tuple[float, float]:
        p = self.params
        base = p.offset_s + k * p.cycle_s
        start = base - self._c(k - 1).early
        c = self._c(k)
        end = base + p.arterial_green_s + c.ext
        if c.checked_out_at is not None and base + p.arterial_green_s <= c.checked_out_at < end:
            end = c.checked_out_at
        return start, end

    def state(self, t: float) -> str:
        """Arterial (and BRT) indication: ``G``, ``Y`` or ``R``."""
        p = self.params
        for k in (self.cycle_of(t), self.cycle_of(t) + 1):
            s, e = self.arterial_window(k)
            if s <= t < e:
                return "G"
            if e <= t < e + p.arterial_yellow_s:
                return "Y"
        return "R"

    def side_green_window(self, k: int) -> tuple[float, float]:
        p = self.params
        _, e = self.arterial_window(k)
        s_next, _ = self.arterial_window(k + 1)
        return e + p.arterial_yellow_s + p.arterial_red_s, s_next - p.side_yellow_s - p.side_red_s

    # -------------------------------------------------------------- priority
    def request(self, now: float, arrival: float, request_id: int) -> Decision:
        """Handle a (possibly repeated / updated) priority request for a bus arriving at ``arrival``."""
        p = self.params
        if not p.enabled:
            return Decision.REJECTED
        target = arrival + p.arrival_buffer_s
        k = self.cycle_of(now)
        for kk in (k, k + 1):
            s, e = self.arterial_window(kk)
            if s <= arrival and target <= e:
                return Decision.NOT_NEEDED
        # --- green extension of the current (or just-ended) arterial green
        c = self._c(k)
        s, e = self.arterial_window(k)
        base_end = p.offset_s + k * p.cycle_s + p.arterial_green_s
        if s <= now < e:  # extension is only possible while the arterial green is still running
            if (not c.locked or c.owner == request_id) and c.checked_out_at is None:
                need = target - base_end
                side_s, side_e = self.side_green_window(k)
                room = (side_e - side_s) + c.ext - p.side_min_green_s
                if 0 < need <= min(p.max_extension_s, room) + 1e-9:
                    c.ext = max(c.ext, need)
                    c.locked, c.owner = True, request_id
                    self._record(now, request_id, Decision.EXTEND, c.ext)
                    return Decision.EXTEND
        # --- early green for the next arterial green
        nxt_s, _ = self.arterial_window(k + 1)
        if now < nxt_s and arrival < nxt_s:
            if (not c.locked or c.owner == request_id):
                side_s, side_e = self.side_green_window(k)
                earliest_cut = max(side_s + p.side_min_green_s, now) + p.side_yellow_s + p.side_red_s
                base_next = p.offset_s + (k + 1) * p.cycle_s
                want = base_next - max(arrival, earliest_cut)
                gain = min(p.max_early_green_s, want, (side_e - side_s) - p.side_min_green_s + c.early)
                if gain > 0.5:
                    c.early = max(c.early, gain)
                    c.locked, c.owner = True, request_id
                    self._record(now, request_id, Decision.EARLY, c.early)
                    return Decision.EARLY
        self._record(now, request_id, Decision.REJECTED, 0.0)
        return Decision.REJECTED

    def check_out(self, now: float, request_id: int) -> None:
        """Bus cleared the stop line: end a running extension that this request owns."""
        k = self.cycle_of(now)
        for kk in (k - 1, k):
            c = self.cycles.get(kk)
            if c and c.owner == request_id and c.ext > 0:
                base_end = self.params.offset_s + kk * self.params.cycle_s + self.params.arterial_green_s
                if base_end <= now < base_end + c.ext:
                    c.checked_out_at = now

    def _record(self, now: float, rid: int, d: Decision, amount: float) -> None:
        self.log.append({"t": round(now, 2), "request": rid, "decision": d.value, "amount_s": round(amount, 2)})

    def priority_seconds(self, k: int) -> tuple[float, float]:
        """Extension actually used (after check-out) and early green granted in cycle ``k``."""
        c = self.cycles.get(k)
        if c is None:
            return 0.0, 0.0
        base_end = self.params.offset_s + k * self.params.cycle_s + self.params.arterial_green_s
        used = c.ext if c.checked_out_at is None else max(0.0, c.checked_out_at - base_end)
        return used, c.early
