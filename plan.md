# Plan: fw v8 Text-Frame Support

Branch: `feat/net-v8-decoder`
Based on: `feat/pump-monitoring-consumption` (will become v1.4.0)
After v1.4.0 is merged to main: `git fetch origin && git rebase origin/main`

---

## Background

Aseko devices with firmware v8 no longer send a 120-byte binary block.
Instead they send a human-readable text frame (example from Issue #49, ASIN AQUA NET):

```
{v1 110203680 804 0 27 ins: 314 -500 -500 -500 0 0 0 0 1 -500 -500 -500 0 24 6 29 21 40 0 ains: 708 708 774 7790 0 0 779 779 0 0 0 0 0 0 0 0 outs: 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 areqs: 74 74 4 5 0 36 36 0 0 0 6 0 36 0 45 0 255 2 2 10 0 15 0 0 0 0 reqs: 0 0 0 0 0 0 0 24 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 10 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 fncs: 0 0 3 0 0 0 2 0 mods: 2 0 0 1 0 0 0 0 flags: 2 0 0 0 0 0 0 0 crc16: C3C8}
```

Frame structure:
- Header: `{v1 <serial> <field2> <field3> <field4>`
- Sections: `ins:` `ains:` `outs:` `areqs:` `reqs:` `fncs:` `mods:` `flags:` `crc16:`
- Delimiters: `{` … `}`
- Size: 463 bytes (vs. 120 bytes for binary frames)

---

## Architecture Decisions

| Topic | Decision | Reason |
|---|---|---|
| Server | Single server, single port | No extra config, no second process |
| Frame detection | After reading 120 bytes: search for `{` | Robust even if TCP delivers mid-frame |
| Text-frame sync | `readuntil(b'}')` once `{` is found | Unambiguous delimiters, no magic-byte guessing |
| Binary-frame sync | Existing `_rewind_frame` logic, renamed to `_rewind_binary` | Logic unchanged |
| Shared sync entry point | `_sync_frame(reader)` | Makes it clear both frame types are handled the same way |
| New decoder | `aseko_decoder_v8.py` / `AsekoV8Decoder` | v8 = firmware architecture version, not device-specific |
| Device type | Reuse `AsekoDeviceType.NET` | Same entities, no HA entity re-registration on update |
| Mirror forwarder | Forward raw bytes unchanged | Aseko Cloud understands the text format itself |
| Forwarder port v7 | `pool.aseko.com:47524` | Existing binary-frame port (unchanged) |
| Forwarder port v8 | `pool.aseko.com:51050` | New text-frame port for fw v8 |
| Port config (user) | No extra options flow fields | Server auto-routes by `FrameType`; ports defined in `const.py` for easy bugfix releases |
| Two devices, mixed fw | Single receiving port on HA side | Each device has its own TCP connection — no data mixing possible; `_sync_frame` operates per-connection |

---

## Step 1 — Pre-Release: Logging & Forwarding for fekberg

**Status:** ⬜ Todo

**Goal:** Build a minimal release that fekberg installs manually.
The server receives v8 text frames, logs them as WARNING (visible without debug mode)
and forwards them to `pool.aseko.com:51050` so his Aseko Cloud keeps working.
No decoding, no new entities.

**This release will not be published publicly** — fekberg installs the files manually.

**Changes for the pre-release:**

1. `aseko_server.py` — add `_sync_frame()` (frame detection + logging only):
   - v8 frame detected → log as WARNING (full raw text)
   - forward v8 frame via forwarder callback (raw bytes)
   - do **not** pass v8 frame to `AsekoDecoder.decode()` (no decoder yet)

2. `mirror_forwarder.py` — make port configuration v8-aware:
   - forward v8 frames to `pool.aseko.com:51050`
   - forward v7 frames to `pool.aseko.com:47524` (unchanged)
   - distinguish by `FrameType` passed alongside the bytes

3. `const.py` — new constant:
   ```python
   DEFAULT_FORWARDER_PORT_V8 = 51050
   ```

**No decoding, no new entities, no tests required for this step.**

---

## Step 2 — Field Mapping (after data collection)

**Status:** ⏳ Blocked — waiting for raw data from fekberg

The sections need to be mapped to `AsekoDevice` fields.
Hypotheses (unconfirmed):

| Section | Index | Hypothesis | AsekoDevice field |
|---|---|---|---|
| Header | 1 | Serial number | `serial_number` |
| `ins:` | 0 | pH (`314` = 3.14?) | `ph` |
| `ins:` | 3 | Temperature? | `water_temperature` |
| `ins:` | 12 | ? | ? |
| `ains:` | 0–1 | Analog inputs | ? |
| `outs:` | 2 | Filtration (`1` = on) | `filtration_pump_running` |
| `areqs:` | 0–1 | pH setpoint? (`74` = 7.4?) | `required_ph` |
| `mods:` | 0 | Operating mode | ? |
| `flags:` | 0 | Status flags | ? |
| `crc16:` | — | CRC16 checksum | — |

**Process:** fekberg installs the pre-release (Step 1) and enables the forwarder.
Logs are collected and compared against Aseko Cloud values.
The table is then completed.

---

## Step 3 — Full `aseko_server.py` refactor

**Status:** ⬜ Todo (depends on Step 2)

**Goal:** Refactor `_handle_client()` to fully support both frame types.

**Changes:**

1. Rename `_rewind_frame()` → `_rewind_binary(data: bytes) -> tuple[bytes, int]`
   - Internal helper, logic unchanged

2. New method `_sync_frame(reader, initial: bytes) -> tuple[bytes, FrameType]`
   - `initial` = the first 120 bytes already read
   - If `b'{'` found in `initial`: text path
     - Locate position of `{`
     - `reader.readuntil(b'}')` for the rest
     - Return complete frame + `FrameType.V8`
   - Otherwise: binary path
     - Call `_rewind_binary(initial)`
     - Return `(rewound_frame, offset)` + `FrameType.BINARY`

3. `_handle_client()` loop:
   - Read initial 120 bytes (unchanged)
   - Call `_sync_frame()`
   - Depending on `FrameType`: call `AsekoDecoder.decode()` or `AsekoV8Decoder.decode()`
   - pH plausibility check only for binary frames (text frames have their own validation)

**New constant in `const.py`** (or as an Enum in `aseko_server.py`):
```python
class FrameType(Enum):
    BINARY = "binary"
    V8 = "v8"
```

---

## Step 4 — Create `aseko_decoder_v8.py`

**Status:** ⬜ Todo (depends on Step 2)

**File:** `custom_components/aseko_local/aseko_decoder_v8.py`

**Class:** `AsekoV8Decoder`

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

**Optional CRC16:** implement validation once field mapping is complete.

---

## Step 5 — Tests

**Status:** ⬜ Todo (depends on Step 4)

**File:** `tests/test_aseko_decoder_v8.py`

Fixture: frame from Issue #49 (above).

Minimum assertions:
- `serial_number == 110203680`
- `device_type == AsekoDeviceType.NET`
- All mapped fields match their expected values

---

## Step 6 — Sensor guards (if needed)

**Status:** ⬜ Todo (after Step 5)

If the v8 frame exposes different sensors than v7 (e.g. no `required_floc`),
guards need to be added in `sensor.py` — same pattern as existing `device_type` checks.

---

## Open Issues

| # | Description | Status |
|---|---|---|
| 1 | Field mapping ins:/ains:/outs: → AsekoDevice | ⏳ Needs raw data from fekberg (after Step 1) |
| 2 | Implement CRC16 validation? | ⏳ Decision pending |
| 3 | Header fields `804 0 27` after serial — meaning? | ⏳ Unknown |
| 4 | Are there v8 frames from other device types (SALT, OXY)? | ⏳ Unknown |
| 5 | Set a max size limit for `readuntil` call? | ⏳ Security review needed |
| 6 | Forwarder port v8: `51050` — confirmed by Aseko? | ⏳ From Issue #49, not yet verified |

---

## Affected Files

| File | Change | From step |
|---|---|---|
| `custom_components/aseko_local/aseko_server.py` | Frame detection + v8 logging (pre-release) → full refactor later | 1 / 3 |
| `custom_components/aseko_local/mirror_forwarder.py` | Port routing v7→47524, v8→51050 | 1 |
| `custom_components/aseko_local/const.py` | `DEFAULT_FORWARDER_PORT_V8 = 51050` | 1 |
| `custom_components/aseko_local/aseko_decoder_v8.py` | NEW | 4 |
| `tests/test_aseko_decoder_v8.py` | NEW | 5 |
| `custom_components/aseko_local/aseko_data.py` | No changes expected | — |
| `custom_components/aseko_local/sensor.py` | Only if sensor guards needed | 6 |
| `README.md` | Forwarder port documentation v7 vs v8 | 1 |
