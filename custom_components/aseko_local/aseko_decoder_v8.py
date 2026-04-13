"""Decoder for Aseko firmware v8 text frames."""

from __future__ import annotations

import logging
import re
from datetime import datetime

import homeassistant.util

from .aseko_data import AsekoDevice, AsekoDeviceType, AsekoProbeType

_LOGGER = logging.getLogger(__name__)

_FRAME_RE = re.compile(
    r"^\{v1\s+"
    r"(?P<serial>\d+)\s+"
    r"(?P<header1>\d+)\s+"
    r"(?P<header2>-?\d+)\s+"
    r"(?P<header3>-?\d+)\s+"
    r"ins:\s+(?P<ins>.*?)\s+"
    r"ains:\s+(?P<ains>.*?)\s+"
    r"outs:\s+(?P<outs>.*?)\s+"
    r"areqs:\s+(?P<areqs>.*?)\s+"
    r"reqs:\s+(?P<reqs>.*?)\s+"
    r"fncs:\s+(?P<fncs>.*?)\s+"
    r"mods:\s+(?P<mods>.*?)\s+"
    r"flags:\s+(?P<flags>.*?)\s+"
    r"crc16:\s+(?P<crc>[0-9A-F]+)\}\s*$"
)

_V8_DEVICE_TYPE_BY_HEADER: dict[int, AsekoDeviceType] = {
    # Observed in the provided v8 NET frames:
    #   {v1 <serial> 804 0 27 ...}
    804: AsekoDeviceType.NET,
}


class AsekoV8Decoder:
    """Decoder for Aseko v8 ASCII telemetry."""

    @staticmethod
    def _parse_int_list(raw: str) -> list[int]:
        return [int(value) for value in raw.split()]

    @staticmethod
    def _optional_int(value: int | None, *, sentinel: int = -500) -> int | None:
        if value is None or value == sentinel:
            return None
        return value

    @staticmethod
    def _unit_type(header1: int) -> AsekoDeviceType | None:
        device_type = _V8_DEVICE_TYPE_BY_HEADER.get(header1)
        if device_type is None:
            _LOGGER.warning("Unknown v8 device header: %s", header1)
        return device_type

    @staticmethod
    def _configuration(ains: list[int]) -> set[AsekoProbeType]:
        probes: set[AsekoProbeType] = {AsekoProbeType.PH}

        # Across the full supplied v8 log:
        # - ains[0]/[1] track the pH value (6.55 .. 6.57)
        # - ains[6]/[7] track the redox value (822 .. 849 mV)
        # No observed field behaves like a free-chlorine probe reading.
        if len(ains) > 6 and ains[6] > 0:
            probes.add(AsekoProbeType.REDOX)

        return probes

    @staticmethod
    def _timestamp(ins: list[int]) -> datetime | None:
        # Observed device clock tuple in supplied frames:
        # ins[13:19] -> year(2-digit), month, day, hour, minute, second
        if len(ins) < 19:
            return None

        try:
            return datetime(
                year=2000 + ins[13],
                month=ins[14],
                day=ins[15],
                hour=ins[16],
                minute=ins[17],
                second=ins[18],
                tzinfo=homeassistant.util.dt.get_default_time_zone(),
            )
        except ValueError:
            _LOGGER.warning("Invalid v8 timestamp tuple: %s", ins[13:19])
            return None

    @classmethod
    def decode(cls, raw: bytes) -> AsekoDevice:
        text = raw.decode("ascii", errors="strict").strip()
        match = _FRAME_RE.match(text)
        if not match:
            raise ValueError("Invalid v8 frame format")

        serial = int(match.group("serial"))
        header1 = int(match.group("header1"))
        ins = cls._parse_int_list(match.group("ins"))
        ains = cls._parse_int_list(match.group("ains"))
        outs = cls._parse_int_list(match.group("outs"))
        areqs = cls._parse_int_list(match.group("areqs"))
        reqs = cls._parse_int_list(match.group("reqs"))

        device_type = cls._unit_type(header1)
        configuration = cls._configuration(ains)

        # Mapping below is based on the supplied v8 NET frames from
        # `home-assistant_2026-04-13T14-13-51.564Z.log`.
        device = AsekoDevice(
            serial_number=serial,
            device_type=device_type,
            configuration=configuration,
            timestamp=cls._timestamp(ins),
            water_temperature=(
                cls._optional_int(ins[0], sentinel=-500) / 10 if len(ins) > 0 else None
            ),
            water_flow_to_probes=(len(ins) > 8 and ins[8] == 1),
            filtration_pump_running=(len(outs) > 2 and bool(outs[2])),
            cl_pump_running=(len(outs) > 0 and bool(outs[0])),
            ph_minus_pump_running=(len(outs) > 1 and bool(outs[1])),
            ph=(ains[0] / 100 if len(ains) > 0 and ains[0] > 0 else None),
            cl_free=None,
            redox=(ains[6] if len(ains) > 6 and ains[6] > 0 else None),
            required_ph=(areqs[0] / 10 if len(areqs) > 0 else None),
            required_cl_free=None,
            required_water_temperature=(reqs[7] if len(reqs) > 7 else None),
            flowrate_ph_minus=(areqs[5] if len(areqs) > 5 and areqs[5] > 0 else None),
            flowrate_chlor=(areqs[6] if len(areqs) > 6 and areqs[6] > 0 else None),
            flowrate_ph_plus=(areqs[12] if len(areqs) > 12 and areqs[12] > 0 else None),
            flowrate_algicide=(
                areqs[14] if len(areqs) > 14 and areqs[14] > 0 else None
            ),
            required_floc=(areqs[19] if len(areqs) > 19 and areqs[19] > 0 else None),
            required_algicide=(
                areqs[21] if len(areqs) > 21 and areqs[21] > 0 else None
            ),
        )

        return device
