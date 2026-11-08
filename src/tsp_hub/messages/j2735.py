"""SAE J2735-style messages with J2735 field units, packed as fixed-layout binary frames.

Fields and units follow the J2735 data elements (``DE_Speed`` 0.02 m/s, ``DE_Heading``
0.0125 deg, ``DE_Latitude``/``DE_Longitude`` 1e-7 deg, ``DE_DSecond`` ms within the minute,
``DE_MsgCount`` 0-127). Over-the-air J2735 uses ASN.1 UPER; this project packs the same
fields with ``struct`` (big-endian, fixed size) so the broker and RSU logic can be exercised
and benchmarked without an ASN.1 toolchain. The wire format is therefore **J2735-shaped,
not J2735-interoperable**.

Frame layout: 1-byte message type, then the payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import ClassVar, Union

from ..core.exceptions import MessageError

SPEED_UNIT = 0.02  # m/s
HEADING_UNIT = 0.0125  # deg
LATLON_UNIT = 1e-7  # deg
SPEED_UNAVAILABLE = 8191
_TYPE_BSM, _TYPE_SRM, _TYPE_SSM = 0x14, 0x1D, 0x1E  # J2735 DSRCmsgID values 20, 29, 30


@dataclass(frozen=True)
class BSM:
    """BSM Part I core data (subset)."""

    msg_count: int
    temp_id: int  # 4-byte temporary ID
    sec_mark_ms: int  # 0..65535 (ms within the minute)
    lat_deg: float
    lon_deg: float
    speed_mps: float
    heading_deg: float
    accel_mps2: float
    length_m: float
    width_m: float
    vehicle_role: int = 0  # 0 basic, 16 transit (J2735 BasicVehicleRole 'transit')

    _FMT: ClassVar[str] = ">BIHiiHHhHHB"

    def __post_init__(self) -> None:
        if not 0 <= self.msg_count <= 127:
            raise MessageError("msgCnt out of range", context={"msg_count": self.msg_count})
        if not 0 <= self.sec_mark_ms <= 65535:
            raise MessageError("secMark out of range")
        if not (-90 <= self.lat_deg <= 90 and -180 <= self.lon_deg <= 180):
            raise MessageError("lat/lon out of range")
        if not 0 <= self.speed_mps <= SPEED_UNAVAILABLE * SPEED_UNIT:
            raise MessageError("speed out of range", context={"speed": self.speed_mps})

    def pack(self) -> bytes:
        return bytes([_TYPE_BSM]) + struct.pack(
            self._FMT, self.msg_count, self.temp_id, self.sec_mark_ms,
            round(self.lat_deg / LATLON_UNIT), round(self.lon_deg / LATLON_UNIT),
            round(self.speed_mps / SPEED_UNIT), round((self.heading_deg % 360.0) / HEADING_UNIT) % 28800,
            round(self.accel_mps2 * 100), round(self.length_m * 100), round(self.width_m * 100), self.vehicle_role,
        )

    @classmethod
    def unpack(cls, payload: bytes) -> "BSM":
        mc, tid, sm, lat, lon, spd, hdg, acc, ln, wd, role = struct.unpack(cls._FMT, payload)
        return cls(mc, tid, sm, lat * LATLON_UNIT, lon * LATLON_UNIT, spd * SPEED_UNIT, hdg * HEADING_UNIT,
                   acc / 100.0, ln / 100.0, wd / 100.0, role)


@dataclass(frozen=True)
class SRM:
    """Signal Request Message: one priority request for one intersection."""

    request_id: int
    vehicle_id: int
    intersection_id: int
    in_approach: int  # 2 = EB, 6 = WB (NEMA phase of the requested movement)
    eta_s: float  # estimated time of arrival at the stop line (s from now)
    eta_window_s: float  # +/- uncertainty
    cancel: bool = False  # check-out / cancellation

    _FMT: ClassVar[str] = ">IIHBHH?"

    def pack(self) -> bytes:
        if not (0 <= self.eta_s < 6553.5 and 0 <= self.eta_window_s < 6553.5):
            raise MessageError("eta out of range")
        return bytes([_TYPE_SRM]) + struct.pack(self._FMT, self.request_id, self.vehicle_id, self.intersection_id,
                                                self.in_approach, round(self.eta_s * 10), round(self.eta_window_s * 10),
                                                self.cancel)

    @classmethod
    def unpack(cls, payload: bytes) -> "SRM":
        rid, vid, iid, app, eta, win, cancel = struct.unpack(cls._FMT, payload)
        return cls(rid, vid, iid, app, eta / 10.0, win / 10.0, cancel)


@dataclass(frozen=True)
class SSM:
    """Signal Status Message: RSU response to a request."""

    request_id: int
    intersection_id: int
    status: int  # 0 received, 1 granted-extension, 2 granted-early-green, 3 rejected, 4 served, 5 cancelled
    _FMT: ClassVar[str] = ">IHB"

    def pack(self) -> bytes:
        return bytes([_TYPE_SSM]) + struct.pack(self._FMT, self.request_id, self.intersection_id, self.status)

    @classmethod
    def unpack(cls, payload: bytes) -> "SSM":
        return cls(*struct.unpack(cls._FMT, payload))


Message = Union[BSM, SRM, SSM]
_DECODERS = {_TYPE_BSM: BSM, _TYPE_SRM: SRM, _TYPE_SSM: SSM}


def encode(msg: Message) -> bytes:
    return msg.pack()


def decode(frame: bytes) -> Message:
    if not frame:
        raise MessageError("empty frame")
    cls = _DECODERS.get(frame[0])
    if cls is None:
        raise MessageError("unknown message type", context={"type": frame[0]})
    try:
        return cls.unpack(frame[1:])  # type: ignore[attr-defined,no-any-return]
    except struct.error as exc:
        raise MessageError(f"bad payload: {exc}", context={"type": frame[0], "len": len(frame)}) from exc
