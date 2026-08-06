"""Diagnostic raw-frame dumper for Aseko devices (Issue #145).

Unlike the Home Assistant debug log — which would log *every* frame (the
device sends one roughly every 10 seconds even when nothing changed) — this
dumper writes one line per **unique** frame per device. A frame is written
only when it differs from the last frame of the same device, so the resulting
file is a chronological list of *state changes* that is small enough to paste
into a GitHub issue.

The dumper handles both wire protocols:

- **v7 binary frames** (120 bytes): rendered as a spaced hex string, device
  serial taken from bytes 0–3 (big-endian).
- **v8 text frames** (``{v1 <serial> …}\\n``): rendered as a one-line ASCII
  string, device serial taken from the header.

One file per device: ``{dump_dir}/aseko_dump_<serial>.md``. The dumper is
toggled at runtime by the options-flow checkbox (``set_enabled``), no code
change needed. Files roll automatically: :meth:`AsekoFrameDumper.cleanup`
removes every dump file whose mtime is older than the retention period, so
after the checkbox is switched off the content disappears on its own.

``/config`` is the HA Core / HA OS / HA Container config directory, the same
level as ``configuration.yaml``, so the file is reachable via VS Code Server,
the File editor add-on, the Samba share, and ``ha core logs`` without extra
setup. Operators on non-``/config`` installs can override via the
``ASEKO_DUMP_DIR`` environment variable.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

_LOGGER = logging.getLogger(__name__)

# Default dump directory used when ASEKO_DUMP_DIR is not set. Same rationale
# as DEFAULT_DUMP_PATH in the old v8-only dumper: `/config` is where
# configuration.yaml lives, so the file is reachable from the usual tools.
DEFAULT_DUMP_DIR: str = "/config"

# v8 text frames start with this signature. Anything else is treated as a
# v7 binary frame.
_V8_SIGNATURE: bytes = b"{v1 "


def _resolve_dump_dir() -> Path:
    """Resolve the directory that holds the per-device dump files.

    Honours the ``ASEKO_DUMP_DIR`` environment variable so an operator can
    redirect captures to a persistent location without touching source.
    """
    return Path(os.environ.get("ASEKO_DUMP_DIR", DEFAULT_DUMP_DIR))


def _frame_to_line(raw: bytes) -> str | None:
    """Return the one-line rendering of a raw frame, or ``None`` if unusable.

    v8 text frames are rendered as a single ASCII line with the trailing
    newline stripped and whitespace collapsed. v7 binary frames are rendered
    as a spaced hex dump. Anything that looks like a text frame (starts with
    ``{``) but is not a parseable ``{v1 …}`` frame returns ``None``.
    """
    if not raw:
        return None
    if raw.startswith(_V8_SIGNATURE):
        try:
            text = raw.decode("ascii", errors="replace").strip()
        except Exception:
            return None
        line = " ".join(text.split())
        return line or None
    if raw.startswith(b"{"):
        return None
    return raw.hex(" ", 1)


class AsekoFrameDumper:
    """Append-only, dedup-by-content dumper for v7 and v8 frames.

    Single-process, thread-safe (the decoder may be called from the asyncio
    event loop on a worker thread, and ``record()`` is called from there).
    One instance per process, accessed via ``get()``; tests use ``reset()``
    to re-bind a fresh instance.

    The dumper keeps a fingerprint of the *last* frame per device serial in
    memory; an incoming frame is appended only when it differs. The enabled
    state is a runtime flag (``set_enabled``) so the options-flow checkbox can
    toggle the dumper without a reload. I/O errors are logged at WARNING and
    swallowed — a failing dump must never crash the integration.
    """

    _instance: "AsekoFrameDumper | None" = None

    def __init__(self, dump_dir: Path) -> None:
        self._dir = dump_dir
        # Whether the options-flow checkbox is on.
        self._enabled: bool = False
        # Per-device cache of the last-written rendering. Keyed by serial so
        # multiple devices (and both frame types) do not collide.
        self._last_per_serial: dict[int, str] = {}
        # Serials whose dump file's Markdown header has been emitted.
        self._header_written: set[int] = set()
        # Diagnostic counters, useful for "did the dumper actually dedup"
        # assertions in tests and for operators reading the HA log.
        self.frames_seen: int = 0
        self.frames_written: int = 0
        self.frames_deduped: int = 0
        self._lock = Lock()

    @classmethod
    def get(cls) -> "AsekoFrameDumper":
        """Return the process-wide singleton, creating it on first use."""
        if cls._instance is None:
            cls._instance = cls(_resolve_dump_dir())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton (test helper)."""
        cls._instance = None

    @property
    def enabled(self) -> bool:
        """Whether the dumper is currently active."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the dumper at runtime."""
        self._enabled = enabled

    def record(self, raw: bytes) -> None:
        """Maybe-append ``raw`` to the device's dump file.

        No-op when the dumper is disabled, when ``raw`` is empty, when the
        frame has no parseable serial, or when the rendering matches the
        last-seen frame for the same device (deduplication).
        """
        if not self._enabled:
            return
        if not raw:
            return

        is_v8 = raw.startswith(_V8_SIGNATURE)
        line = _frame_to_line(raw)
        if not line:
            return

        serial = self._extract_serial(raw)
        if serial is None:
            return  # unparseable header — skip silently

        with self._lock:
            self.frames_seen += 1
            if not self._has_changed(serial, line):
                self.frames_deduped += 1
                return
            self._last_per_serial[serial] = line
            self.frames_written += 1
            self._append(serial, line, "v8" if is_v8 else "v7")

    def _has_changed(self, serial: int, line: str) -> bool:
        """Return True iff ``line`` differs from the cached frame for ``serial``.

        Always True for a serial that has not been seen before (the first
        frame of a session is always written as the baseline).
        """
        return self._last_per_serial.get(serial) != line

    @staticmethod
    def _extract_serial(raw: bytes) -> int | None:
        """Return the device serial, or ``None`` if it cannot be determined.

        v8: second whitespace-separated token of the ``{v1 <serial> …}``
        header. v7: bytes 0–3 interpreted as a big-endian integer. Frames
        that start with ``{`` but are not a parseable ``{v1 …}`` frame are
        treated as unparseable and return ``None``.
        """
        if raw.startswith(_V8_SIGNATURE):
            parts = raw.decode("ascii", errors="replace").split(maxsplit=3)
            if len(parts) < 2:
                return None
            try:
                return int(parts[1])
            except ValueError:
                return None
        if raw.startswith(b"{"):
            return None
        if len(raw) < 4:
            return None
        return int.from_bytes(raw[0:4], "big")

    def _path_for(self, serial: int) -> Path:
        """Return the dump file path for a device serial."""
        return self._dir / f"aseko_dump_{serial}.md"

    def _append(self, serial: int, line: str, frame_type: str) -> None:
        """Write one record line to the device's dump file.

        Creates the file (and the Markdown header) on the first record ever
        written for this device. Subsequent records are appended as-is. If the
        operator deletes the file out from under us between records, the next
        write recreates it with the header. I/O errors are logged and
        swallowed.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out_line = f"| `{ts}` | `{serial}` | {frame_type} | `{line}` |"
        path = self._path_for(serial)
        try:
            need_header = serial not in self._header_written or not path.exists()
            if need_header:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as f:
                    f.write(self._header() + "\n")
                if serial not in self._header_written:
                    _LOGGER.info("Aseko dump started for serial %s at %s", serial, path)
                self._header_written.add(serial)
            with path.open("a", encoding="utf-8") as f:
                f.write(out_line + "\n")
        except OSError as exc:
            _LOGGER.warning("Could not write dump file %s: %s", path, exc)

    def cleanup(self, retention: timedelta) -> None:
        """Delete dump files whose mtime is older than ``retention``.

        This is the rolling-deletion safety net: an active dumper keeps its
        file's mtime fresh on every write, so it is never deleted mid-analysis;
        after the checkbox is switched off the mtime freezes and the file
        disappears once it is older than the retention period.
        """
        cutoff = time.time() - retention.total_seconds()
        for path in list(self._dir.glob("aseko_dump_*.md")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    _LOGGER.info(
                        "Aseko dump rolled (older than %s): %s", retention, path
                    )
            except OSError as exc:
                _LOGGER.warning("Could not roll dump file %s: %s", path, exc)

    @staticmethod
    def _header() -> str:
        """Return the Markdown header written on the first record of a file."""
        return (
            "# Aseko raw-frame dump\n"
            "\n"
            "One line per *unique* frame (in-memory dedup per serial).\n"
            "v7 rows show the 120-byte binary frame as spaced hex; v8 rows\n"
            "show the raw text frame. Use `python scripts/v8_tools.py\n"
            "decode_frame <line>` (or the `AsekoV8Decoder.decode(raw)` Python\n"
            "API) to inspect a v8 line.\n"
            "\n"
            "| timestamp | serial | type | frame |\n"
            "|-----------|--------|------|-------|"
        )
