from custom_components.aseko_local.aseko_data import AsekoDeviceType, AsekoProbeType
from custom_components.aseko_local.aseko_decoder_v8 import AsekoV8Decoder

V8_FRAME = (
    b"{v1 123456789 804 0 27 ins: 188 -500 -500 -500 0 0 0 0 1 -500 -500 -500 "
    b"0 25 1 24 14 49 0 ains: 655 655 817 8220 0 0 822 822 0 0 0 0 0 0 0 0 "
    b"outs: 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"areqs: 74 74 4 5 0 36 36 0 0 0 6 0 36 0 45 0 255 2 2 10 0 15 0 0 0 0 "
    b"reqs: 0 0 0 0 0 0 0 24 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"0 10 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"fncs: 0 0 3 0 0 0 2 0 mods: 2 0 0 1 0 0 0 0 flags: 2 0 0 0 0 0 0 0 crc16: DD41}\n"
)

V8_LATE_FRAME = (
    b"{v1 123456789 804 0 27 ins: 185 -500 -500 -500 0 0 0 0 1 -500 -500 -500 "
    b"0 25 1 24 16 11 0 ains: 657 657 844 8490 0 0 849 849 0 0 0 0 0 0 0 0 "
    b"outs: 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"areqs: 74 74 4 5 0 36 36 0 0 0 6 0 36 0 45 0 255 2 2 10 0 15 0 0 0 0 "
    b"reqs: 0 0 0 0 0 0 0 24 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"0 10 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 "
    b"fncs: 0 0 3 0 0 0 2 0 mods: 2 0 0 1 0 0 0 0 flags: 2 0 0 0 0 0 0 0 crc16: 5BB0}\n"
)


def test_decode_v8_frame_from_log() -> None:
    device = AsekoV8Decoder.decode(V8_FRAME)

    assert device.serial_number == 123456789
    assert device.device_type == AsekoDeviceType.NET
    assert device.configuration == {
        AsekoProbeType.PH,
        AsekoProbeType.REDOX,
    }

    assert device.water_temperature == 18.8
    assert device.water_flow_to_probes is True
    assert device.filtration_pump_running is True
    assert device.cl_pump_running is False
    assert device.ph_minus_pump_running is False

    assert device.ph == 6.55
    assert device.cl_free is None
    assert device.redox == 822

    assert device.required_ph == 7.4
    assert device.required_cl_free is None
    assert device.required_water_temperature == 24
    assert device.flowrate_ph_minus == 36
    assert device.flowrate_chlor == 36
    assert device.flowrate_ph_plus == 36
    assert device.flowrate_algicide == 45
    assert device.required_floc == 10
    assert device.required_algicide == 15


def test_decode_v8_late_frame_matches_observed_ui_values() -> None:
    device = AsekoV8Decoder.decode(V8_LATE_FRAME)

    assert device.ph == 6.57
    assert device.redox == 849
    assert device.required_ph == 7.4
