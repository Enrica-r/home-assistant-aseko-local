# consumption_scan.py
import struct

# === PARAMETER ANPASSEN ===
DUMP_FILE = "dumps_2025_09_10.txt"  # Name deiner Dump-Datei
APP_CHLOR_L = 0.14  # Verbrauch Chlor laut App in Litern
APP_PHMINUS_L = 0.03  # Verbrauch pH- laut App in Litern
# ==========================


def parse_hex_dump(line: str) -> bytes:
    """Extrahiere die reinen Bytes aus einer Logzeile."""
    parts = line.strip().split()
    return bytes.fromhex(parts[1])  # zweites Feld ist der Hex-Dump


def delta_u16(start: int, end: int) -> int:
    """Berechne Differenz mit 16bit Wraparound."""
    return (end - start) & 0xFFFF


def delta_u32(start: int, end: int) -> int:
    """Berechne Differenz mit 32bit Wraparound."""
    return (end - start) & 0xFFFFFFFF


def is_monotonic_u16(values):
    """Prüfe, ob 16-bit Werte monoton steigen (mit Wraparound)."""
    prev = values[0]
    for cur in values[1:]:
        if delta_u16(prev, cur) < 0:
            return False
        prev = cur
    return True


def is_monotonic_u32(values):
    """Prüfe, ob 32-bit Werte monoton steigen (mit Wraparound)."""
    prev = values[0]
    for cur in values[1:]:
        if delta_u32(prev, cur) < 0:
            return False
        prev = cur
    return True


def batch_scan(filename: str, app_liters: float, label: str):
    """Scanne Datei nach allen möglichen 16/32bit-Countern und finde beste Faktoren."""
    with open(filename, "r") as f:
        lines = f.readlines()

    if len(lines) < 2:
        print(f"[{label}] Datei hat zu wenige Zeilen!")
        return

    frames = [parse_hex_dump(line) for line in lines]

    start = frames[0]
    end = frames[-1]

    best = []
    for i in range(len(start) - 3):  # bis 32bit möglich
        # --- 16bit ---
        vals16 = [struct.unpack_from(">H", fr, i)[0] for fr in frames]
        if is_monotonic_u16(vals16):
            delta16 = delta_u16(vals16[0], vals16[-1])
            if delta16 > 0:
                factor16 = app_liters / delta16
                est = delta16 * factor16
                best.append((i, "u16", delta16, factor16, est))

        # --- 32bit ---
        vals32 = [struct.unpack_from(">I", fr, i)[0] for fr in frames]
        if is_monotonic_u32(vals32):
            delta32 = delta_u32(vals32[0], vals32[-1])
            if delta32 > 0:
                factor32 = app_liters / delta32
                est = delta32 * factor32
                best.append((i, "u32", delta32, factor32, est))

    # sortiere nach Genauigkeit (Differenz zum App-Wert)
    best.sort(key=lambda x: abs(app_liters - x[4]))

    print(f"\n=== Ergebnisse für {label} (App: {app_liters:.3f} L) ===")
    for i, (pos, typ, delta, factor, est) in enumerate(best[:10], 1):
        print(
            f"{i:2d}. Pos={pos:02d} Typ={typ} Δ={delta} "
            f"Faktor={factor:.8f} → Verbrauch={est:.3f} L"
        )


if __name__ == "__main__":
    batch_scan(DUMP_FILE, APP_CHLOR_L, "Chlor")
    batch_scan(DUMP_FILE, APP_PHMINUS_L, "pH-")
