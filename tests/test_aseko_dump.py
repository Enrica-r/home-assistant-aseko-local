"""Tests for the raw-frame dumper (aseko_dumper.py).

Pins the diff-only behaviour (one line per *unique* frame per device), the
per-device dump files, the runtime enable/disable flag and the rolling
cleanup/retention:

  * A disabled dumper is a no-op.
  * Identical frames are deduplicated per serial.
  * Any byte change produces exactly one new line.
  * v7 binary frames are rendered as spaced hex, serial from bytes 0-3.
  * v8 text frames are rendered as ASCII, serial from the header.
  * One dump file per device: ``aseko_dump_<serial>.md``.
  * ``ASEKO_DUMP_DIR`` overrides the default directory.
  * ``cleanup(retention)`` removes files whose mtime is older than retention.
"""

import os
import time
from datetime import timedelta

import pytest

from custom_components.aseko_local.aseko_dumper import (
    DEFAULT_DUMP_DIR,
    AsekoFrameDumper,
    _frame_to_line,
    _resolve_dump_dir,
)


# ---------- fixtures & helpers ---------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dumper_singleton():
    """Reset the process-wide dumper singleton between tests."""
    AsekoFrameDumper.reset()
    yield
    AsekoFrameDumper.reset()


@pytest.fixture
def dump_dir(tmp_path):
    """A fresh dump directory inside pytest's tmp_path."""
    return tmp_path / "dumps"


def _v8_frame(*sections: str, serial: int = 110215844, f2: int = 100) -> bytes:
    """Build a v8 text frame for testing."""
    body = f"v1 {serial} {f2} 0 31 " + " ".join(sections)
    return ("{" + body + "}\n").encode("ascii")


def _v7_frame(serial: int = 110215844) -> bytes:
    """Build a 120-byte v7 binary frame for testing."""
    frame = bytearray(120)
    frame[0:4] = serial.to_bytes(4, "big")
    frame[29] = 0x02  # some payload so frames differ from a zeroed frame
    return bytes(frame)


def _active_dumper(dump_dir: object) -> AsekoFrameDumper:
    """Return an enabled dumper bound to ``dump_dir``."""
    dumper = AsekoFrameDumper(dump_dir)
    dumper.set_enabled(True)
    return dumper


def _record_lines(path: object) -> list[str]:
    """Return the table body lines (timestamp rows) of a dump file."""
    contents = path.read_text(encoding="utf-8")
    return [ln for ln in contents.splitlines() if ln.startswith("| `20")]


# ---------- default path & env override -----------------------------------


def test_default_dump_dir_is_config():
    """`DEFAULT_DUMP_DIR` is `/config` so the files land next to
    `configuration.yaml` and are reachable via VS Code Server / Samba share /
    File editor add-on.
    """
    assert DEFAULT_DUMP_DIR == "/config"


def test_resolve_dump_dir_prefers_env(monkeypatch, tmp_path):
    """`ASEKO_DUMP_DIR` env var overrides the default directory."""
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("ASEKO_DUMP_DIR", str(custom))
    assert _resolve_dump_dir() == custom


def test_resolve_dump_dir_falls_back_to_default(monkeypatch):
    """Without the env var, the default `/config` wins."""
    from pathlib import Path

    monkeypatch.delenv("ASEKO_DUMP_DIR", raising=False)
    assert _resolve_dump_dir() == Path(DEFAULT_DUMP_DIR)


# ---------- rendering & serial extraction ---------------------------------


def test_frame_to_line_v8_collapses_whitespace():
    """A v8 line must be a single line with single spaces."""
    raw = b"{v1 123 100 0 31   ins:  1   2   3   }\n"
    line = _frame_to_line(raw)
    assert "\n" not in line
    assert "  " not in line
    assert line.startswith("{v1 123 100 0 31 ins: 1 2 3 }")


def test_frame_to_line_v7_is_spaced_hex():
    """A v7 binary frame is rendered as a spaced hex dump."""
    raw = bytes.fromhex("00000007ff")
    assert _frame_to_line(raw) == "00 00 00 07 ff"


def test_frame_to_line_returns_none_on_empty():
    """Empty input is unusable."""
    assert _frame_to_line(b"") is None


def test_extract_serial_v8_reads_header():
    """The serial comes from the second whitespace-separated token."""
    assert AsekoFrameDumper._extract_serial(_v8_frame(serial=110215844)) == 110215844
    assert AsekoFrameDumper._extract_serial(_v8_frame(serial=999)) == 999
    assert AsekoFrameDumper._extract_serial(b"{v1}") is None
    assert AsekoFrameDumper._extract_serial(b"{v1 notanumber 100 0 31 x}\n") is None


def test_extract_serial_v7_reads_first_four_bytes():
    """The serial is bytes 0-3 interpreted as big-endian."""
    assert AsekoFrameDumper._extract_serial(_v7_frame(serial=42)) == 42
    assert AsekoFrameDumper._extract_serial(b"\x01\x02\x03\x04") == 0x01020304
    assert AsekoFrameDumper._extract_serial(b"\x01\x02") is None


# ---------- runtime enable/disable ---------------------------------------


def test_disabled_dumper_is_no_op(dump_dir):
    """A disabled dumper writes nothing and does not count frames."""
    dumper = AsekoFrameDumper(dump_dir)
    dumper.set_enabled(False)
    dumper.record(_v8_frame("ins: 0"))
    dumper.record(_v7_frame())
    assert not any(dump_dir.glob("*"))
    assert dumper.frames_seen == 0
    assert dumper.frames_written == 0
    assert dumper.enabled is False


def test_set_enabled_toggles_runtime_flag(dump_dir):
    """`set_enabled` flips the runtime flag without a reload."""
    dumper = AsekoFrameDumper(dump_dir)
    assert dumper.enabled is False
    dumper.set_enabled(True)
    assert dumper.enabled is True


# ---------- dedup: the headline behaviour --------------------------------


def test_identical_v8_frames_are_deduplicated(dump_dir):
    """Ten identical v8 frames produce ONE record line, not ten."""
    dumper = _active_dumper(dump_dir)
    frame = _v8_frame(
        "ins: 200 0 0 0 0 0 0 0 1 0 0 0 0 24 6 29 10 28 0",
        "outs: 0 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
    )
    for _ in range(10):
        dumper.record(frame)
    assert dumper.frames_seen == 10
    assert dumper.frames_written == 1
    assert dumper.frames_deduped == 9
    path = dump_dir / "aseko_dump_110215844.md"
    assert len(_record_lines(path)) == 1


def test_identical_v7_frames_are_deduplicated(dump_dir):
    """Identical v7 frames are deduplicated just like v8 frames."""
    dumper = _active_dumper(dump_dir)
    frame = _v7_frame(serial=42)
    for _ in range(5):
        dumper.record(frame)
    assert dumper.frames_seen == 5
    assert dumper.frames_written == 1
    assert dumper.frames_deduped == 4
    path = dump_dir / "aseko_dump_42.md"
    assert len(_record_lines(path)) == 1


def test_one_byte_change_produces_one_new_line(dump_dir):
    """A single byte change in any section triggers exactly one new line."""
    dumper = _active_dumper(dump_dir)
    base = _v8_frame(
        "ins: 200 0 0 0 0 0 0 0 1 0 0 0 0 24 6 29 10 28 0",
        "outs: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
    )
    changed = _v8_frame(
        "ins: 200 0 0 0 0 0 0 0 1 0 0 0 0 24 6 29 10 28 0",
        "outs: 0 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
    )
    dumper.record(base)
    dumper.record(base)  # duplicate → deduped
    dumper.record(changed)  # changed → written
    dumper.record(changed)  # duplicate → deduped
    assert dumper.frames_seen == 4
    assert dumper.frames_written == 2  # baseline + change
    assert dumper.frames_deduped == 2
    path = dump_dir / "aseko_dump_110215844.md"
    assert len(_record_lines(path)) == 2


def test_dedup_is_per_serial(dump_dir):
    """Two devices on the same instance do not collide with each other."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=111111111))
    dumper.record(_v8_frame("ins: 0", serial=222222222))
    dumper.record(_v8_frame("ins: 0", serial=111111111))  # same as first
    dumper.record(_v8_frame("ins: 0", serial=222222222))  # same as second
    assert dumper.frames_written == 2
    assert dumper.frames_deduped == 2


# ---------- per-device files ---------------------------------------------


def test_one_file_per_serial(dump_dir):
    """Each device serial gets its own dump file."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=111111111))
    dumper.record(_v8_frame("ins: 0", serial=222222222))
    files = sorted(p.name for p in dump_dir.iterdir())
    assert files == ["aseko_dump_111111111.md", "aseko_dump_222222222.md"]


def test_header_written_once_per_file(dump_dir):
    """The Markdown header is emitted exactly once per device file."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=42))
    dumper.record(_v8_frame("ins: 0 0", serial=42))
    path = dump_dir / "aseko_dump_42.md"
    contents = path.read_text(encoding="utf-8")
    assert "# Aseko raw-frame dump" in contents
    assert "| timestamp | serial | type | frame |" in contents
    assert contents.count("# Aseko raw-frame dump") == 1
    assert len(_record_lines(path)) == 2


def test_record_line_carries_serial_type_and_payload(dump_dir):
    """Each record line includes timestamp, serial, type and the raw frame."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v7_frame(serial=42))
    line = _record_lines(dump_dir / "aseko_dump_42.md")[0]
    assert "`42`" in line
    assert "| v7 |" in line
    assert "00 00 00 2a" in line  # serial 42 = 0x0000002a in big-endian
    dumper.record(_v8_frame("ins: 0", serial=43))
    line = _record_lines(dump_dir / "aseko_dump_43.md")[0]
    assert "`43`" in line
    assert "v8" in line
    assert "ins: 0" in line


def test_header_re_emitted_if_operator_deletes_file(dump_dir):
    """If the operator `rm`'s the file, the next record re-emits the header."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=42))
    path = dump_dir / "aseko_dump_42.md"
    path.unlink()
    dumper.record(_v8_frame("ins: 0 0 0", serial=42))
    contents = path.read_text(encoding="utf-8")
    assert "# Aseko raw-frame dump" in contents  # header re-emitted
    assert len(_record_lines(path)) == 1  # only the post-deletion record


# ---------- robustness ----------------------------------------------------


def test_dumper_no_op_for_empty_or_unparseable_frames(dump_dir):
    """Empty frames and frames without a serial are silently skipped."""
    dumper = _active_dumper(dump_dir)
    dumper.record(b"")
    dumper.record(b"{notv1 1 2 3 4}\n")
    assert not any(dump_dir.glob("*"))


def test_dumper_swallows_oserror(dump_dir, monkeypatch):
    """A failing write must not crash the integration; counters still update."""
    dumper = _active_dumper(dump_dir)

    def _broken_open(self, *args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr("pathlib.PosixPath.open", _broken_open)
    dumper.record(_v8_frame("ins: 0"))  # must NOT raise
    assert dumper.frames_written == 1
    assert not any(dump_dir.glob("*"))


# ---------- rolling cleanup / retention ----------------------------------


def test_cleanup_deletes_files_older_than_retention(dump_dir):
    """Files whose mtime is older than the retention are removed."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=111))
    dumper.record(_v8_frame("ins: 0", serial=222))
    path_old = dump_dir / "aseko_dump_111.md"
    path_fresh = dump_dir / "aseko_dump_222.md"
    old = time.time() - timedelta(days=3).total_seconds()
    os.utime(path_old, (old, old))
    dumper.cleanup(timedelta(days=2))
    assert not path_old.exists()
    assert path_fresh.exists()


def test_cleanup_keeps_fresh_files(dump_dir):
    """An actively written file (fresh mtime) is never deleted."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=42))
    path = dump_dir / "aseko_dump_42.md"
    dumper.cleanup(timedelta(days=2))
    assert path.exists()


def test_cleanup_uses_retention_window(dump_dir):
    """The retention window decides how old is too old."""
    dumper = _active_dumper(dump_dir)
    dumper.record(_v8_frame("ins: 0", serial=42))
    path = dump_dir / "aseko_dump_42.md"
    one_day = time.time() - timedelta(days=1).total_seconds()
    os.utime(path, (one_day, one_day))
    dumper.cleanup(timedelta(days=2))  # 1 day old < 2 days → kept
    assert path.exists()
    dumper.cleanup(timedelta(hours=12))  # 1 day old > 12 h → removed
    assert not path.exists()
