# scan_counters.py — run locally with two frames and app_liters
from typing import List, Tuple


def uint_from(bs: bytes, off: int, length: int) -> int:
    if off + length > len(bs):
        return None
    return int.from_bytes(bs[off : off + length], "big")


def delta_wrap_unsigned(cur: int, prev: int, bits: int) -> int:
    if prev is None:
        return 0
    modulo = 1 << bits
    d = (cur - prev) & (modulo - 1)
    # interpret as forward movement (0..modulo-1)
    return d


def scan_frames(
    frame_before: bytes,
    frame_after: bytes,
    app_liters: float,
    start: int = 0x20,
    end: int = 0x80,
) -> List[Tuple]:
    """
    Scans offsets start..end (byte indexes) for 16- and 32-bit counters,
    computes raw delta and implied L/count.
    Returns list of tuples: (offset, bits, before, after, delta, implied_LperCount, implied_factor_description)
    """
    results = []
    for off in range(start, end):
        # 16-bit
        a16 = uint_from(frame_before, off, 2)
        b16 = uint_from(frame_after, off, 2)
        if a16 is not None and b16 is not None:
            d16 = delta_wrap_unsigned(b16, a16, 16)
            if d16 > 0:
                lpc = app_liters / d16
            else:
                lpc = None
            results.append((off, 16, a16, b16, d16, lpc))
        # 32-bit (only if space)
        a32 = uint_from(frame_before, off, 4)
        b32 = uint_from(frame_after, off, 4)
        if a32 is not None and b32 is not None:
            d32 = delta_wrap_unsigned(b32, a32, 32)
            if d32 > 0:
                lpc32 = app_liters / d32
            else:
                lpc32 = None
            results.append((off, 32, a32, b32, d32, lpc32))

    # filter and sort: prefer small plausible L/count (not nan), prefer small offsets
    filtered = [r for r in results if r[4] > 0 and r[5] is not None]

    # compute plausibility metric: prefer 1e-6..1e-1 L/count (µL..100mL per count)
    def score(r):
        lpc = r[5]
        # ideal range between 1e-6 and 1e-1
        if lpc is None:
            return 999
        if lpc < 1e-6 or lpc > 1e-1:
            return abs(math.log10(lpc) - math.log10(1e-4)) + 10
        # closer to 1e-3..1e-2 preferred
        return abs(math.log10(lpc) - math.log10(1e-3))

    import math

    filtered_sorted = sorted(filtered, key=score)
    return filtered_sorted


# Example usage:
frame_before = bytes.fromhex(
    "069187240901ffffffffffff000402da002affff0031ff06e00155ff000000a80000000000ff00b4069187240903ffffffffffff470a08ffffffffffffffffff02930155ffffffffffffffffffffff14069187240902ffffffffffff0001003cffff003cffff010383ff00781e02581e28ffffffff0049a9"
)  # put your raw hex (no spaces or with .split)

frame_after = bytes.fromhex(
    "069187240901ffffffffffff000402d40024ffff0026ff003f0148ff0000046e0000000000ff00a5069187240903ffffffffffff470a08ffffffffffffffffff028d0148ffffffffffffffffffffff17069187240902ffffffffffff0001003cffff003cffff010383ff00781e02581e28ffffffff0049a9"
)

app_liters = 0.03
candidates = scan_frames(frame_before, frame_after, app_liters)
for c in candidates[:30]:
    off, bits, a, b, d, lpc = c
    print(
        f"off={off:02X} bits={bits} before={a} after={b} delta={d} implied_LperCount={lpc:.9f}"
    )
