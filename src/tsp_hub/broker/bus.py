"""Two broker implementations with the same publish/subscribe contract.

* :class:`SimBroker` runs on **simulated time**: each published frame is delivered after a
  channel latency, or dropped with a packet-loss probability, using a seeded RNG. Studies
  are therefore reproducible, and the RSU logic sees exactly what a lossy radio channel
  would deliver.
* :class:`AsyncBroker` runs on **wall-clock time** with ``asyncio``: one bounded queue per
  subscriber, a drop-oldest policy under back-pressure (a slow consumer never blocks
  producers, and stale BSMs are the right thing to lose), and delivery-latency metrics.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ..core.exceptions import BrokerError

Handler = Callable[[str, bytes, float], None]


@dataclass(frozen=True)
class ChannelModel:
    """Radio channel: fixed latency + exponential jitter, independent packet loss."""

    latency_s: float = 0.02
    jitter_s: float = 0.005
    loss: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.loss < 1.0:
            raise BrokerError("loss must be in [0, 1)")


@dataclass
class SimBroker:
    channel: ChannelModel = field(default_factory=ChannelModel)
    seed: int = 0
    _subs: dict[str, list[Handler]] = field(default_factory=lambda: defaultdict(list))
    _pending: list[tuple[float, int, str, bytes]] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=itertools.count)
    published: int = 0
    dropped: int = 0
    delivered: int = 0

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def publish(self, topic: str, frame: bytes, now: float) -> None:
        self.published += 1
        if self.channel.loss and self._rng.random() < self.channel.loss:
            self.dropped += 1
            return
        delay = self.channel.latency_s + (self._rng.exponential(self.channel.jitter_s) if self.channel.jitter_s else 0.0)
        heapq.heappush(self._pending, (now + delay, next(self._seq), topic, frame))

    def advance(self, now: float) -> None:
        """Deliver every frame whose arrival time is <= ``now`` (in arrival order)."""
        while self._pending and self._pending[0][0] <= now + 1e-12:
            t_arr, _, topic, frame = heapq.heappop(self._pending)
            for h in self._subs.get(topic, ()):
                h(topic, frame, t_arr)
                self.delivered += 1


class AsyncBroker:
    """In-process asyncio pub/sub with bounded per-subscriber queues (drop-oldest)."""

    def __init__(self, queue_size: int = 1024) -> None:
        self.queue_size = queue_size
        self._subs: dict[str, list[asyncio.Queue[tuple[float, bytes]]]] = defaultdict(list)
        self._closed = False
        self.published = 0
        self.dropped_overflow = 0

    def subscribe(self, topic: str) -> asyncio.Queue[tuple[float, bytes]]:
        if self._closed:
            raise BrokerError("broker closed")
        q: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue(self.queue_size)
        self._subs[topic].append(q)
        return q

    def publish(self, topic: str, frame: bytes) -> int:
        """Non-blocking publish; returns the number of subscribers reached."""
        if self._closed:
            raise BrokerError("broker closed")
        self.published += 1
        stamp = time.perf_counter()
        n = 0
        for q in self._subs.get(topic, ()):
            if q.full():
                q.get_nowait()
                self.dropped_overflow += 1
            q.put_nowait((stamp, frame))
            n += 1
        return n

    def close(self) -> None:
        self._closed = True

    def topics(self) -> list[str]:
        return sorted(self._subs)
