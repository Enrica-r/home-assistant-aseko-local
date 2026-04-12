# Plan: fw v8 Text-Frame Support

Branch: `feat/net-v8-decoder`
Basis: `feat/pump-monitoring-consumption` (wird zu v1.4.0)
Nach Release v1.4.0: `git fetch origin && git rebase origin/main`

---

## Hintergrund

Aseko-Geräte mit Firmware v8 senden keinen 120-Byte-Binärblock mehr, sondern
einen menschenlesbaren Text-Frame nach folgendem Schema (Beispiel aus Issue #49
von einem ASIN AQUA NET):

```
{v1 110203680 804 0 27 ins: 314 -500 -500 -500 0 0 0 0 1 -500 -500 -500 0 24 6 29 21 40 0 ains: 708 708 774 7790 0 0 779 779 0 0 0 0 0 0 0 0 outs: 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 areqs: 74 74 4 5 0 36 36 0 0 0 6 0 36 0 45 0 255 2 2 10 0 15 0 0 0 0 reqs: 0 0 0 0 0 0 0 24 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 10 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 fncs: 0 0 3 0 0 0 2 0 mods: 2 0 0 1 0 0 0 0 flags: 2 0 0 0 0 0 0 0 crc16: C3C8}
```

Frame-Struktur:
- Header: `{v1 <serial> <field2> <field3> <field4>`
- Sections: `ins:` `ains:` `outs:` `areqs:` `reqs:` `fncs:` `mods:` `flags:` `crc16:`
- Delimiter: `{` … `}`
- Grösse: 463 Bytes (vs. 120 Bytes beim Binary-Frame)

---

## Architektur-Entscheidungen

| Thema | Entscheidung | Begründung |
|---|---|---|
| Server | Ein Server, ein Port | Kein Config-Aufwand, kein zweiter Prozess |
| Frame-Erkennung | Nach 120-Byte-Read: `{` suchen | Robust auch wenn TCP mitten im Frame startet |
| Sync Text-Frame | `readuntil(b'}')` nach `{` gefunden | Eindeutige Delimiters, kein Marker-Raten nötig |
| Sync Binary-Frame | Bestehende `_rewind_frame` Logik, Rename zu `_rewind_binary` | Keine Änderung der Logik |
| Gemeinsame Sync-Funktion | `_sync_frame(reader)` als einziger Einstieg | Entwickler sieht auf einen Blick: beide Typen gleich behandelt |
| Neuer Decoder | `aseko_decoder_v8.py` / `AsekoV8Decoder` | v8 = Architekturversion, nicht gerätespezifisch |
| Device Type | `AsekoDeviceType.NET` wiederverwenden | Gleiche Entities, keine HA-Entity-Re-Registration bei Update |
| Mirror Forwarder | Raw Bytes unverändert weiterleiten | Aseko Cloud versteht das Text-Format selbst |

---

## Schritt 1 — Field Mapping (vor Implementierung)

**Status:** ⏳ Offen — braucht Daten von Winnetoux

Die Sections müssen den `AsekoDevice`-Feldern zugeordnet werden.
Hypothesen (noch unbestätigt):

| Section | Index | Hypothese | AsekoDevice-Feld |
|---|---|---|---|
| Header | 1 | Serial Number | `serial_number` |
| `ins:` | 0 | pH (`314` = 3.14?) | `ph` |
| `ins:` | 3 | Temperatur? | `water_temperature` |
| `ins:` | 12 | ? | ? |
| `ains:` | 0–1 | Analog-Inputs | ? |
| `outs:` | 2 | Filtration (`1` = an) | `filtration_pump_running` |
| `areqs:` | 0–1 | pH-Sollwert? (`74` = 7.4?) | `required_ph` |
| `mods:` | 0 | Betriebsmodus | ? |
| `flags:` | 0 | Status-Flags | ? |
| `crc16:` | — | CRC16 Prüfsumme | — |

**Vorgehen:** Winnetoux aktiviert Raw-Logging, sendet Frame + Aseko-Cloud-Screenshot.
Dann werden Werte verglichen und Tabelle vervollständigt.

---

## Schritt 2 — `aseko_server.py` anpassen

**Status:** ⬜ Todo

**Ziel:** `_handle_client()` so umbauen, dass beide Frame-Typen unterstützt werden.

**Änderungen:**

1. Rename `_rewind_frame()` → `_rewind_binary(data: bytes) -> tuple[bytes, int]`
   - Interne Hilfsfunktion, Logik identisch

2. Neue Methode `_sync_frame(reader, initial: bytes) -> tuple[bytes, FrameType]`
   - `initial` = die ersten 120 Bytes (bereits gelesen)
   - Falls `b'{'` in `initial`: Text-Pfad
     - Position von `{` bestimmen
     - `reader.readuntil(b'}')` für den Rest
     - Vollständigen Frame zurückgeben + `FrameType.V8`
   - Sonst: Binary-Pfad
     - `_rewind_binary(initial)` aufrufen
     - `(rewound_frame, offset)` + `FrameType.BINARY` zurückgeben

3. `_handle_client()` Loop:
   - Liest initial 120 Bytes (unverändert)
   - Ruft `_sync_frame()` auf
   - Je nach `FrameType`: `AsekoDecoder.decode()` oder `AsekoV8Decoder.decode()`
   - pH-Plausibilitätsprüfung nur für Binary-Frames (Text-Frames haben eigene Validierung)

**Neue Konstante in `const.py`** (oder als Enum in `aseko_server.py`):
```python
class FrameType(Enum):
    BINARY = "binary"
    V8 = "v8"
```

---

## Schritt 3 — `aseko_decoder_v8.py` erstellen

**Status:** ⬜ Todo (abhängig von Schritt 1)

**Datei:** `custom_components/aseko_local/aseko_decoder_v8.py`

**Klasse:** `AsekoV8Decoder`

```python
class AsekoV8Decoder:
    @classmethod
    def decode(cls, raw: bytes) -> AsekoDevice:
        text = raw.decode("ascii")
        # Parse header: {v1 <serial> ...
        # Parse sections: ins:, ains:, outs:, areqs:, reqs:, fncs:, mods:, flags:, crc16:
        # Map to AsekoDevice fields
        # Return AsekoDevice(...)
```

**Optionales CRC16:** Validierung implementieren sobald Felder gemappt sind.

---

## Schritt 4 — Tests

**Status:** ⬜ Todo (abhängig von Schritt 3)

**Datei:** `tests/test_aseko_decoder_v8.py`

Fixture: Frame aus Issue #49 (oben).

Mindest-Asserts:
- `serial_number == 110203680`
- `device_type == AsekoDeviceType.NET`
- Alle gemappten Felder auf ihre erwarteten Werte

---

## Schritt 5 — Sensor-Guards (falls nötig)

**Status:** ⬜ Todo (nach Schritt 4)

Falls der v8-Frame andere Sensoren liefert als v7 (z.B. kein `required_floc`),
müssen Guards in `sensor.py` ergänzt werden — analog zu bestehenden `device_type`-Checks.

---

## Issues / Offene Fragen

| # | Beschreibung | Status |
|---|---|---|
| 1 | Field Mapping ins:/ains:/outs: → AsekoDevice | ⏳ Braucht Testdaten von Winnetoux |
| 2 | CRC16-Validierung implementieren? | ⏳ Entscheidung offen |
| 3 | Header-Felder `804 0 27` nach Serial — Bedeutung? | ⏳ Unbekannt |
| 4 | Gibt es v8-Frames von anderen Geräten (SALT, OXY)? | ⏳ Unbekannt |
| 5 | Maximale Frame-Grösse für `readuntil`-Limit setzen? | ⏳ Sicherheits-Review |

---

## Betroffene Files

| File | Änderung |
|---|---|
| `custom_components/aseko_local/aseko_server.py` | `_sync_frame()`, Rename `_rewind_binary()`, `_handle_client()` Loop |
| `custom_components/aseko_local/aseko_decoder_v8.py` | NEU |
| `tests/test_aseko_decoder_v8.py` | NEU |
| `custom_components/aseko_local/aseko_data.py` | Keine Änderungen erwartet |
| `custom_components/aseko_local/sensor.py` | Nur falls Sensor-Guards nötig (Schritt 5) |
