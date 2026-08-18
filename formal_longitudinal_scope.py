from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import math
import os
import re
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyvisa


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "formal_scope_batches.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "scope_runs"
MONTHS = {
    name: number
    for number, name in enumerate(
        ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )
    if name
}
TIMESTAMP_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})\s+(?P<year>\d{4})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\."
    r"(?P<f1>\d{3})\s+(?P<f2>\d{3})\s+(?P<f3>\d{3})\s+(?P<f4>\d{3})"
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def atomic_save_npy(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        np.save(handle, array, allow_pickle=False)
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def make_attempt_dir(root: Path, run_id: str, batch_id: str, category: str) -> Path:
    parent = root / run_id / category / batch_id
    parent.mkdir(parents=True, exist_ok=True)
    attempt_number = 1
    while (parent / f"attempt_{attempt_number:03d}").exists():
        attempt_number += 1
    attempt = parent / f"attempt_{attempt_number:03d}"
    attempt.mkdir()
    return attempt


def frame_map_for_batch(
    batch: dict[str, Any], shots_per_point: int | None = None
) -> list[dict[str, Any]]:
    configured_shots = int(batch["shots_per_point"])
    shots = configured_shots if shots_per_point is None else shots_per_point
    rows: list[dict[str, Any]] = []
    if shots_per_point is None:
        for point in batch["points"]:
            ranges = point.get("frame_ranges_1based")
            if ranges is None:
                ranges = [point["frames_1based"]]
            point_frames: list[int] = []
            for frame_range in ranges:
                first, last = map(int, frame_range)
                if last < first:
                    raise ValueError(f"{point['point_id']} has a reversed frame range")
                point_frames.extend(range(first, last + 1))
            point_frames.sort()
            if len(point_frames) != len(set(point_frames)):
                raise ValueError(f"{point['point_id']} has overlapping frame ranges")
            if len(point_frames) != configured_shots:
                raise ValueError(
                    f"{point['point_id']} has {len(point_frames)} frames, "
                    f"expected {configured_shots}"
                )
            for shot, frame in enumerate(point_frames, start=1):
                rows.append(
                    {
                        "logical_frame_1based": frame,
                        "point_id": point["point_id"],
                        "shot_in_point_1based": shot,
                        "discard": frame in batch.get("discard_frames", []),
                        "point": point,
                    }
                )
    else:
        frame = 1
        for point in batch["points"]:
            for shot in range(1, shots + 1):
                rows.append(
                    {
                        "logical_frame_1based": frame,
                        "point_id": point["point_id"],
                        "shot_in_point_1based": shot,
                        "discard": False,
                        "point": point,
                    }
                )
                frame += 1
    rows.sort(key=lambda row: row["logical_frame_1based"])
    frames = [row["logical_frame_1based"] for row in rows]
    if frames != list(range(1, len(rows) + 1)):
        raise ValueError("Frame mapping must cover a contiguous 1-based range")
    return rows


def validate_contract(contract: dict[str, Any]) -> None:
    if float(contract["awg_sample_rate_hz"]) != 2.0e9:
        raise ValueError("Unexpected AWG sample rate")
    expected_period = float(contract["awg_sram_samples"]) / float(
        contract["awg_sample_rate_hz"]
    )
    if not math.isclose(expected_period, float(contract["awg_period_s"]), abs_tol=1e-15):
        raise ValueError("AWG period does not match SRAM length/sample rate")
    if int(contract["marker_rising_edges_per_period"]) != 1:
        raise ValueError("Formal capture requires exactly one AUX marker rise per period")
    for key, batch in contract["batches"].items():
        rows = frame_map_for_batch(batch)
        if len(rows) != int(batch["total_frames"]):
            raise ValueError(f"{key}: total_frames does not match frame map")
        if batch.get("discard_frames", []) != []:
            raise ValueError(f"{key}: unexpected discard frames in the handed-off contract")
        center = float(batch["digital_center_ns"])
        left, right = map(float, batch["digital_coverage_ns"])
        half_span = 0.5 * float(batch["scope_span_ns"])
        if left < center - half_span - 1e-9 or right > center + half_span + 1e-9:
            raise ValueError(f"{key}: digital coverage does not fit requested scope span")


class ScopeIO:
    def __init__(self, resource: str, timeout_ms: int = 10000):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.rm = None
        self.scope = None
        self.transcript: list[dict[str, Any]] = []

    def __enter__(self) -> "ScopeIO":
        self.rm = pyvisa.ResourceManager()
        self.scope = self.rm.open_resource(self.resource)
        self.scope.timeout = self.timeout_ms
        self.scope.chunk_size = 4 * 1024 * 1024
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.scope is not None:
            self.scope.close()
        if self.rm is not None:
            self.rm.close()

    def write(self, command: str) -> None:
        assert self.scope is not None
        self.scope.write(command)
        self.transcript.append(
            {"time": iso_now(), "operation": "write", "command": command}
        )

    def query(self, command: str, *, log_response: bool = True) -> str:
        assert self.scope is not None
        response = self.scope.query(command).strip()
        item: dict[str, Any] = {
            "time": iso_now(),
            "operation": "query",
            "command": command,
        }
        if log_response:
            item["response"] = response[:2000]
            if len(response) > 2000:
                item["response_truncated"] = True
        self.transcript.append(item)
        return response

    def query_float(self, command: str) -> float:
        return float(self.query(command))

    def query_int(self, command: str) -> int:
        return int(float(self.query(command)))

    def query_binary(self, command: str, datatype: str, big_endian: bool) -> np.ndarray:
        assert self.scope is not None
        started = time.monotonic()
        result = self.scope.query_binary_values(
            command,
            datatype=datatype,
            is_big_endian=big_endian,
            container=np.array,
        )
        self.transcript.append(
            {
                "time": iso_now(),
                "operation": "query_binary",
                "command": command,
                "items": int(result.size),
                "elapsed_s": time.monotonic() - started,
            }
        )
        return result

    def safe_stop(self) -> None:
        if self.scope is None:
            return
        for command in ("ACQUIRE:STATE STOP", "HORIZONTAL:FASTFRAME:STATE OFF"):
            try:
                self.write(command)
            except Exception:
                pass


def query_scope_snapshot(io: ScopeIO) -> dict[str, str]:
    commands = {
        "idn": "*IDN?",
        "id": "ID?",
        "header": "HEADER?",
        "fastacq": "FASTACQ:STATE?",
        "acquisition_state": "ACQUIRE:STATE?",
        "sampling_mode": "ACQUIRE:SAMPLINGMODE?",
        "acquisition_mode": "ACQUIRE:MODE?",
        "horizontal_mode": "HORIZONTAL:MODE?",
        "sample_rate": "HORIZONTAL:MODE:SAMPLERATE?",
        "record_length": "HORIZONTAL:MODE:RECORDLENGTH?",
        "horizontal_scale": "HORIZONTAL:MODE:SCALE?",
        "horizontal_delay_mode": "HORIZONTAL:DELAY:MODE?",
        "horizontal_delay_time": "HORIZONTAL:DELAY:TIME?",
        "horizontal_delay_position": "HORIZONTAL:DELAY:POSITION?",
        "interpolation_ratio": "HORIZONTAL:MAIN:INTERPRATIO?",
        "ch3_termination": "CH3:TERMINATION?",
        "ch3_coupling": "CH3:COUPLING?",
        "ch3_bandwidth": "CH3:BANDWIDTH?",
        "ch3_scale": "CH3:SCALE?",
        "ch3_position": "CH3:POSITION?",
        "ch3_offset": "CH3:OFFSET?",
        "trigger_source": "TRIGGER:A:EDGE:SOURCE?",
        "trigger_slope": "TRIGGER:A:EDGE:SLOPE:AUX?",
        "aux_level": "TRIGGER:AUXLEVEL?",
        "trigger_mode": "TRIGGER:A:MODE?",
        "fastframe_state": "HORIZONTAL:FASTFRAME:STATE?",
        "fastframe_count": "HORIZONTAL:FASTFRAME:COUNT?",
        "fastframe_sequence": "HORIZONTAL:FASTFRAME:SEQUENCE?",
        "fastframe_sumframe": "HORIZONTAL:FASTFRAME:SUMFRAME?",
        "frames_acquired": "ACQUIRE:NUMFRAMESACQUIRED?",
    }
    result: dict[str, str] = {}
    for key, command in commands.items():
        try:
            result[key] = io.query(command)
        except Exception as exc:
            result[key] = f"ERROR: {type(exc).__name__}: {exc}"
    return result


def read_event_status(io: ScopeIO, stage: str) -> dict[str, str]:
    result = {"stage": stage, "time": iso_now()}
    try:
        result["esr"] = io.query("*ESR?")
    except Exception as exc:
        result["esr"] = f"ERROR: {exc}"
    try:
        result["events"] = io.query("ALLEV?")
    except Exception as exc:
        result["events"] = f"ERROR: {exc}"
    return result


def configure_scope(
    io: ScopeIO,
    contract: dict[str, Any],
    batch: dict[str, Any],
    *,
    frame_count: int,
    path_delay_ns: float,
    span_ns: float,
    vertical_scale_v: float,
    vertical_offset_v: float,
) -> dict[str, Any]:
    defaults = contract["scope_defaults"]
    sample_rate = float(defaults["sample_rate_hz"])
    record_length = int(round(span_ns * 1e-9 * sample_rate))
    delay_s = (float(batch["digital_center_ns"]) + path_delay_ns) * 1e-9

    io.write("ACQUIRE:STATE STOP")
    io.write("HORIZONTAL:FASTFRAME:STATE OFF")
    io.write("*CLS")
    io.write("HEADER OFF")
    io.write("FASTACQ:STATE OFF")
    io.write("ACQUIRE:SAMPLINGMODE RT")
    io.write("ACQUIRE:MODE SAMPLE")
    try:
        io.write("ACQUIRE:INTERPEIGHTBIT OFF")
    except Exception:
        pass

    io.write("SELECT:CH3 ON")
    io.write("CH3:TERMINATION 50")
    io.write("CH3:COUPLING DC")
    io.write("CH3:BANDWIDTH FULL")
    io.write(f"CH3:SCALE {vertical_scale_v:.12e}")
    io.write("CH3:POSITION 0")
    io.write(f"CH3:OFFSET {vertical_offset_v:.12e}")

    io.write("TRIGGER:A:TYPE EDGE")
    io.write("TRIGGER:A:EDGE:SOURCE AUXILIARY")
    io.write("TRIGGER:A:EDGE:SLOPE:AUX RISE")
    io.write(f"TRIGGER:AUXLEVEL {float(defaults['trigger_level_v']):.12e}")
    io.write("TRIGGER:A:MODE NORMAL")

    io.write("HORIZONTAL:MODE MANUAL")
    io.write(f"HORIZONTAL:MODE:SAMPLERATE {sample_rate:.12e}")
    io.write(f"HORIZONTAL:MODE:RECORDLENGTH {record_length}")
    io.write("HORIZONTAL:DELAY:MODE ON")
    io.write("HORIZONTAL:DELAY:POSITION 50")
    io.write("HORIZONTAL:POSITION 0")
    io.write(f"HORIZONTAL:DELAY:TIME {delay_s:.15e}")
    io.write("ZOOM:STATE OFF")

    io.write("HORIZONTAL:FASTFRAME:STATE ON")
    maximum_frames = io.query_int("HORIZONTAL:FASTFRAME:MAXFRAMES?")
    if maximum_frames < frame_count:
        raise RuntimeError(
            f"FastFrame capacity {maximum_frames} is smaller than requested {frame_count}"
        )
    io.write(f"HORIZONTAL:FASTFRAME:COUNT {frame_count}")
    io.write("HORIZONTAL:FASTFRAME:SEQUENCE FIRST")
    io.write("HORIZONTAL:FASTFRAME:SUMFRAME NONE")

    io.write("DATA:SOURCE CH3")
    io.write("DATA:START 1")
    io.write(f"DATA:STOP {record_length}")
    io.write("DATA:FRAMESTART 1")
    io.write(f"DATA:FRAMESTOP {frame_count}")
    io.write("DATA:ENCDG SRIbinary")
    io.write("WFMOUTPRE:BYT_NR 1")

    effective = query_scope_snapshot(io)
    effective.update(
        {
            "requested_frame_count": frame_count,
            "maximum_frames": maximum_frames,
            "requested_path_delay_ns": path_delay_ns,
            "requested_digital_center_ns": float(batch["digital_center_ns"]),
            "requested_scope_delay_s": delay_s,
            "requested_span_ns": span_ns,
            "requested_record_length": record_length,
            "requested_vertical_scale_v_per_div": vertical_scale_v,
            "requested_vertical_offset_v": vertical_offset_v,
        }
    )

    actual_rate = float(effective["sample_rate"])
    actual_length = int(float(effective["record_length"]))
    actual_delay = float(effective["horizontal_delay_time"])
    if not math.isclose(actual_rate, sample_rate, rel_tol=0, abs_tol=1.0):
        raise RuntimeError(f"Scope sample rate is {actual_rate}, expected {sample_rate}")
    if actual_length != record_length:
        raise RuntimeError(f"Scope record length is {actual_length}, expected {record_length}")
    # The delay readback is quantized on this firmware. Accept at most roughly
    # half one 40 ps acquisition interval, and preserve the actual readback.
    if not math.isclose(actual_delay, delay_s, rel_tol=0, abs_tol=25e-12):
        raise RuntimeError(f"Scope delay is {actual_delay}, expected {delay_s}")
    if int(float(effective["fastframe_count"])) != frame_count:
        raise RuntimeError("FastFrame count readback does not match request")
    if effective["fastframe_sequence"].upper() != "FIRST":
        raise RuntimeError("FastFrame SEQUENCE is not FIRST")
    if effective["fastframe_sumframe"].upper() != "NONE":
        raise RuntimeError("FastFrame SUMFRAME is not NONE")
    return effective


def arm_and_wait_for_ready(io: ScopeIO, ready_timeout_s: float) -> None:
    io.write("ACQUIRE:STOPAFTER SEQUENCE")
    io.write("ACQUIRE:STATE RUN")
    deadline = time.monotonic() + ready_timeout_s
    time.sleep(0.4)
    while time.monotonic() < deadline:
        if io.query_int("TRIGGER:A:READY?") == 1:
            return
        if io.query_int("ACQUIRE:STATE?") == 0:
            raise RuntimeError("Acquisition stopped before reaching READY")
        time.sleep(0.05)
    raise TimeoutError("Scope did not report TRIGGER:A:READY? = 1")


def wait_for_frames(
    io: ScopeIO, expected_frames: int, capture_timeout_s: float
) -> dict[str, Any]:
    deadline = time.monotonic() + capture_timeout_s
    last_count = -1
    history: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        count = io.query_int("ACQUIRE:NUMFRAMESACQUIRED?")
        state = io.query_int("ACQUIRE:STATE?")
        busy = io.query_int("BUSY?")
        if count != last_count:
            print(f"FastFrame progress: {count}/{expected_frames}", flush=True)
            history.append(
                {"time": iso_now(), "frames": count, "acquisition_state": state, "busy": busy}
            )
            last_count = count
        if count == expected_frames and state == 0 and busy == 0:
            return {
                "frames_acquired": count,
                "acquisition_state": state,
                "busy": busy,
                "progress_history": history,
            }
        if count > expected_frames:
            raise RuntimeError(f"Scope acquired {count} frames, expected {expected_frames}")
        time.sleep(0.2)
    count = io.query_int("ACQUIRE:NUMFRAMESACQUIRED?")
    raise TimeoutError(f"Timed out with {count}/{expected_frames} frames acquired")


def waveform_preamble(io: ScopeIO) -> dict[str, Any]:
    text_queries = {
        "BYT_NR": "WFMOUTPRE:BYT_NR?",
        "BIT_NR": "WFMOUTPRE:BIT_NR?",
        "BN_FMT": "WFMOUTPRE:BN_FMT?",
        "BYT_OR": "WFMOUTPRE:BYT_OR?",
        "PT_FMT": "WFMOUTPRE:PT_FMT?",
        "XUNIT": "WFMOUTPRE:XUNIT?",
        "YUNIT": "WFMOUTPRE:YUNIT?",
        "WFID": "WFMOUTPRE:WFID?",
    }
    number_queries = {
        "NR_PT": "WFMOUTPRE:NR_PT?",
        "NR_FR": "WFMOUTPRE:NR_FR?",
        "XINCR": "WFMOUTPRE:XINCR?",
        "XZERO_SELECTED": "WFMOUTPRE:XZERO?",
        "PT_OFF": "WFMOUTPRE:PT_OFF?",
        "YMULT": "WFMOUTPRE:YMULT?",
        "YOFF": "WFMOUTPRE:YOFF?",
        "YZERO": "WFMOUTPRE:YZERO?",
    }
    result: dict[str, Any] = {}
    for key, command in text_queries.items():
        result[key] = io.query(command).strip('"')
    for key, command in number_queries.items():
        result[key] = io.query_float(command)
    result["NR_PT"] = int(round(result["NR_PT"]))
    result["NR_FR"] = int(round(result["NR_FR"]))
    result["BYT_NR"] = int(float(result["BYT_NR"]))
    result["BIT_NR"] = int(float(result["BIT_NR"]))
    return result


def binary_format(preamble: dict[str, Any]) -> tuple[str, bool]:
    byte_count = int(preamble["BYT_NR"])
    signed = str(preamble["BN_FMT"]).upper() == "RI"
    if byte_count == 1:
        datatype = "b" if signed else "B"
    elif byte_count == 2:
        datatype = "h" if signed else "H"
    else:
        raise RuntimeError(f"Unsupported waveform byte count: {byte_count}")
    big_endian = str(preamble["BYT_OR"]).upper().startswith("MSB")
    if str(preamble["PT_FMT"]).upper() != "Y":
        raise RuntimeError(f"Expected PT_FMT=Y, received {preamble['PT_FMT']}")
    return datatype, big_endian


def download_fastframes(
    io: ScopeIO, frame_count: int
) -> tuple[np.ndarray, dict[str, Any], np.ndarray, list[str]]:
    io.write("DATA:SOURCE CH3")
    io.write("DATA:FRAMESTART 1")
    io.write(f"DATA:FRAMESTOP {frame_count}")
    preamble = waveform_preamble(io)
    if preamble["NR_FR"] != frame_count:
        raise RuntimeError(
            f"WFMOUTPRE:NR_FR is {preamble['NR_FR']}, expected {frame_count}"
        )
    datatype, big_endian = binary_format(preamble)
    assert io.scope is not None
    old_timeout = io.scope.timeout
    io.scope.timeout = 120000
    try:
        flat = io.query_binary("CURVE?", datatype, big_endian)
    finally:
        io.scope.timeout = old_timeout
    expected_items = frame_count * int(preamble["NR_PT"])
    if flat.size != expected_items:
        raise RuntimeError(
            f"CURVE returned {flat.size} items; expected {expected_items}"
        )
    raw = flat.reshape(frame_count, int(preamble["NR_PT"]))

    xzero = np.empty(frame_count, dtype=float)
    timestamps: list[str] = []
    for frame in range(1, frame_count + 1):
        xzero[frame - 1] = io.query_float(
            f"HORIZONTAL:FASTFRAME:XZERO:FRAME:CH3? {frame}"
        )
        timestamps.append(
            io.query(f"HORIZONTAL:FASTFRAME:TIMESTAMP:FRAME:CH3? {frame}").strip('"')
        )
    return raw, preamble, xzero, timestamps


def timestamp_decimal(raw: str) -> Decimal | None:
    match = TIMESTAMP_RE.search(raw)
    if not match:
        return None
    groups = match.groupdict()
    month = MONTHS.get(groups["month"].title())
    if month is None:
        return None
    second_base = calendar.timegm(
        (
            int(groups["year"]),
            month,
            int(groups["day"]),
            int(groups["hour"]),
            int(groups["minute"]),
            int(groups["second"]),
        )
    )
    fraction = Decimal("0." + groups["f1"] + groups["f2"] + groups["f3"] + groups["f4"])
    return Decimal(second_base) + fraction


def build_time_and_voltage(
    raw: np.ndarray, preamble: dict[str, Any], xzero: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    indices = np.arange(raw.shape[1], dtype=float)
    time_axes = xzero[:, None] + float(preamble["XINCR"]) * (
        indices[None, :] - float(preamble["PT_OFF"])
    )
    voltage = float(preamble["YZERO"]) + float(preamble["YMULT"]) * (
        raw.astype(float) - float(preamble["YOFF"])
    )
    return time_axes, voltage


def point_for_frame(mapping: list[dict[str, Any]], frame: int) -> dict[str, Any]:
    return mapping[frame - 1]


def common_grid(time_axes: np.ndarray, voltage: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    start = float(np.max(time_axes[:, 0]))
    stop = float(np.min(time_axes[:, -1]))
    dt = float(np.median(np.diff(time_axes, axis=1)))
    count = int(math.floor((stop - start) / dt)) + 1
    grid = start + dt * np.arange(count)
    aligned = np.stack([np.interp(grid, t, v) for t, v in zip(time_axes, voltage)])
    return grid, aligned


def point_timing_offset_ns(point: dict[str, Any]) -> float:
    """Return the realized command offset used for deterministic timing alignment."""
    for key in (
        "realized_timing_lag_offset_ns",
        "realized_timinglag_offset_ns",
        "timing_lag_offset_ns",
        "requested_timing_lag_offset_ns",
    ):
        if key in point:
            return float(Decimal(str(point[key])))
    raise KeyError(
        f"{point.get('point_id', '<unknown>')} has no realized timing-lag offset"
    )


def save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        atomic_write_text(path, "")
        return
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def save_capture(
    attempt: Path,
    contract: dict[str, Any],
    batch_key: str,
    batch: dict[str, Any],
    mapping: list[dict[str, Any]],
    raw: np.ndarray,
    preamble: dict[str, Any],
    xzero: np.ndarray,
    timestamps: list[str],
    effective_scope: dict[str, Any],
    path_delay_ns: float,
    path_delay_source: str,
    acquisition_status: dict[str, Any],
    event_status: list[dict[str, str]],
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_dir = attempt / "raw"
    derived_dir = attempt / "derived"
    plots_dir = attempt / "plots"
    raw_dir.mkdir(exist_ok=True)
    derived_dir.mkdir(exist_ok=True)
    plots_dir.mkdir(exist_ok=True)

    time_axes, voltage = build_time_and_voltage(raw, preamble, xzero)
    atomic_save_npy(attempt / "raw_frames.npy", raw)
    atomic_save_npy(attempt / "frame_xzero_s.npy", xzero)
    atomic_write_json(attempt / "preamble_common.json", preamble)
    atomic_write_json(attempt / "fastframe_timestamps.json", timestamps)

    parsed = [timestamp_decimal(item) for item in timestamps]
    deltas: list[float | None] = [None]
    for previous, current in zip(parsed, parsed[1:]):
        if previous is None or current is None:
            deltas.append(None)
        else:
            deltas.append(float(current - previous))

    frame_rows: list[dict[str, Any]] = []
    point_groups: dict[str, list[int]] = {}
    write_per_frame_files = len(mapping) <= 1000
    for frame_index, map_row in enumerate(mapping):
        frame = frame_index + 1
        point = map_row["point"]
        point_id = map_row["point_id"]
        point_groups.setdefault(point_id, []).append(frame_index)
        raw_path = raw_dir / f"frame_{frame:04d}_ch3.npy"
        meta_path = raw_dir / f"frame_{frame:04d}_meta.json"
        if write_per_frame_files:
            atomic_save_npy(raw_path, raw[frame_index])
        signed_limit = 2 ** (int(preamble["BIT_NR"]) - 1)
        clipping = bool(
            str(preamble["BN_FMT"]).upper() == "RI"
            and (
                int(raw[frame_index].min()) <= -signed_limit + 1
                or int(raw[frame_index].max()) >= signed_limit - 2
            )
        )
        meta = {
            "logical_frame_1based": frame,
            "instrument_frame_selector": frame,
            "planned_point_id": point_id,
            "validated_point_id": None,
            "shot_in_point_1based": map_row["shot_in_point_1based"],
            "discard": map_row["discard"],
            "point_parameters": point,
            "xzero_s": float(xzero[frame_index]),
            "timestamp_raw": timestamps[frame_index],
            "delta_previous_trigger_s": deltas[frame_index],
            "raw_min": int(raw[frame_index].min()),
            "raw_max": int(raw[frame_index].max()),
            "voltage_min_v": float(voltage[frame_index].min()),
            "voltage_max_v": float(voltage[frame_index].max()),
            "clipping": clipping,
            "received_points": int(raw.shape[1]),
            "preamble": {
                **preamble,
                "XZERO": float(xzero[frame_index]),
            },
            "preamble_common_file": "../preamble_common.json",
            "mapping_basis": "contract_positional_1based",
        }
        if write_per_frame_files:
            atomic_write_json(meta_path, meta)
            raw_sha = sha256_file(raw_path)
            meta_sha = sha256_file(meta_path)
        else:
            raw_sha = ""
            meta_sha = ""
        frame_rows.append(
            {
                "logical_frame_1based": frame,
                "instrument_frame_selector": frame,
                "planned_point_id": point_id,
                "validated_point_id": "",
                "shot_in_point_1based": map_row["shot_in_point_1based"],
                "discard": map_row["discard"],
                "timestamp_raw": timestamps[frame_index],
                "delta_previous_trigger_s": "" if deltas[frame_index] is None else deltas[frame_index],
                "xzero_s": float(xzero[frame_index]),
                "raw_min": int(raw[frame_index].min()),
                "raw_max": int(raw[frame_index].max()),
                "clipping": clipping,
                "received_points": int(raw.shape[1]),
                "raw_path": str(raw_path.relative_to(attempt)) if write_per_frame_files else "",
                "raw_sha256": raw_sha,
                "meta_path": str(meta_path.relative_to(attempt)) if write_per_frame_files else "",
                "meta_sha256": meta_sha,
                "mapping_status": "planned_unverified_awg_log",
            }
        )
    save_csv(attempt / "frame_index.csv", frame_rows)

    point_summaries: list[dict[str, Any]] = []
    raw_mean_curves: list[tuple[str, np.ndarray, np.ndarray]] = []
    aligned_mean_curves: list[tuple[str, np.ndarray, np.ndarray]] = []
    for point in batch["points"]:
        point_id = point["point_id"]
        indices = point_groups.get(point_id, [])
        if not indices:
            continue
        point_times = time_axes[indices]
        point_voltage = voltage[indices]
        grid, aligned = common_grid(point_times, point_voltage)
        mean = np.mean(aligned, axis=0)
        std = np.std(aligned, axis=0)
        experiment = str(batch["experiment"]).lower()
        if experiment == "gatelen":
            shift_s = (float(point["target_center_ns"]) + path_delay_ns) * 1e-9
            aligned_axis = grid - shift_s
            alignment_kind = "programmed_tfall_center_plus_fixed_path_delay"
        elif experiment in {"timinglag", "timing_precision", "timing_response"}:
            shift_s = point_timing_offset_ns(point) * 1e-9
            aligned_axis = grid - shift_s
            alignment_kind = "commanded_timinglag_only"
        elif experiment == "amplitude_precision":
            aligned_axis = grid.copy()
            alignment_kind = "aux_trigger_only_no_timing_shift"
        else:
            raise ValueError(f"Unsupported experiment type: {batch['experiment']}")
        point_path = derived_dir / f"{point_id}.npz"
        np.savez_compressed(
            point_path,
            frame_indices_1based=np.asarray(indices, dtype=int) + 1,
            raw=raw[indices],
            voltage=point_voltage,
            time_aux_relative_s=point_times,
            common_time_aux_relative_s=grid,
            mean_voltage=mean,
            std_voltage=std,
            aligned_time_s=aligned_axis,
            alignment_kind=alignment_kind,
            no_free_shift=True,
        )
        raw_mean_curves.append((point_id, grid, mean))
        aligned_mean_curves.append((point_id, aligned_axis, mean))
        point_summaries.append(
            {
                "point_id": point_id,
                "frames": len(indices),
                "mean_baseline_v": float(np.median(aligned[:, : max(1, aligned.shape[1] // 10)])),
                "mean_peak_to_peak_v": float(np.ptp(mean)),
                "within_point_mean_std_v": float(np.mean(std)),
                "alignment_kind": alignment_kind,
                "free_shift_used": False,
                "derived_file": str(point_path.relative_to(attempt)),
            }
        )
    save_csv(attempt / "point_summary.csv", point_summaries)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    for point_id, axis, curve in raw_mean_curves:
        axes[0].plot(axis * 1e9, curve * 1e3, lw=1.2, label=point_id)
    axes[0].set_title("AUX-trigger-aligned point means (no fitted shift)")
    axes[0].set_xlabel("Time relative to AUX trigger (ns)")
    axes[0].set_ylabel("CH3 (mV)")
    axes[0].grid(True)
    if len(batch["points"]) <= 24:
        axes[0].legend(fontsize=8, ncol=2)
    for point_id, axis, curve in aligned_mean_curves:
        axes[1].plot(axis * 1e9, curve * 1e3, lw=1.2, label=point_id)
    experiment = str(batch["experiment"]).lower()
    if experiment == "gatelen":
        axes[1].set_title("Programmed tfall-center alignment using one fixed path delay")
        axes[1].set_xlabel("Time from programmed tfall center + fixed path delay (ns)")
    elif experiment in {"timinglag", "timing_precision", "timing_response"}:
        axes[1].set_title("Command-corrected overlay (subtract timingLagMeas only)")
        axes[1].set_xlabel("AUX-relative time minus commanded timingLagMeas (ns)")
    else:
        axes[1].set_title("AUX-trigger-aligned amplitude scan (no fitted time shift)")
        axes[1].set_xlabel("Time relative to AUX trigger (ns)")
    axes[1].set_ylabel("CH3 (mV)")
    axes[1].grid(True)
    if len(batch["points"]) <= 24:
        axes[1].legend(fontsize=8, ncol=2)
    fig.savefig(plots_dir / "point_mean_overlays.png", dpi=180)
    plt.close(fig)

    timestamp_parse_ok = all(item is not None for item in parsed)
    finite_deltas = [value for value in deltas[1:] if value is not None]
    timestamp_monotonic = bool(finite_deltas) and all(value > 0 for value in finite_deltas)
    minimum_interval = float(contract["awg_period_s"]) * 0.95
    trigger_spacing_ok = bool(finite_deltas) and all(
        value >= minimum_interval for value in finite_deltas
    )
    clipping_frames = [row["logical_frame_1based"] for row in frame_rows if row["clipping"]]
    coverage_failures: list[int] = []
    for frame_index, map_row in enumerate(mapping):
        point = map_row["point"]
        expected_start = (float(point["target_start_ns"]) + path_delay_ns) * 1e-9
        expected_end = (float(point["target_end_ns"]) + path_delay_ns) * 1e-9
        if expected_start < time_axes[frame_index, 0] or expected_end > time_axes[frame_index, -1]:
            coverage_failures.append(frame_index + 1)

    no_event_errors = all(
        (
            str(item.get("esr", "")).strip() == "0"
            and (
                "no events" in str(item.get("events", "")).lower()
                or "queue empty" in str(item.get("events", "")).lower()
            )
        )
        for item in event_status
    )
    sample_rate_ok = math.isclose(
        float(effective_scope["sample_rate"]), 25.0e9, rel_tol=0, abs_tol=1.0
    )
    record_length_ok = int(float(effective_scope["record_length"])) == raw.shape[1]
    sample_spacing_ok = math.isclose(
        float(preamble["XINCR"]), 40.0e-12, rel_tol=0, abs_tol=0.1e-12
    )
    interpolation_ratio_ok = math.isclose(
        float(effective_scope["interpolation_ratio"]), 1.0, rel_tol=0, abs_tol=1e-9
    )

    scope_checks = {
        "frame_count_ok": raw.shape[0] == len(mapping),
        "points_per_frame_ok": raw.shape[1] == int(preamble["NR_PT"]),
        "xzero_all_finite": bool(np.all(np.isfinite(xzero))),
        "timestamp_parse_ok": timestamp_parse_ok,
        "timestamp_strictly_monotonic": timestamp_monotonic,
        "minimum_trigger_spacing_ok": trigger_spacing_ok,
        "minimum_allowed_trigger_spacing_s": minimum_interval,
        "minimum_observed_trigger_spacing_s": min(finite_deltas) if finite_deltas else None,
        "clipping_frames": clipping_frames,
        "target_coverage_failures": coverage_failures,
        "sample_rate_25_gs_per_s": sample_rate_ok,
        "record_length_matches_transfer": record_length_ok,
        "sample_spacing_40_ps": sample_spacing_ok,
        "interpolation_ratio_one": interpolation_ratio_ok,
        "scpi_event_queue_clean": no_event_errors,
    }
    scope_valid = all(
        (
            scope_checks["frame_count_ok"],
            scope_checks["points_per_frame_ok"],
            scope_checks["xzero_all_finite"],
            scope_checks["timestamp_parse_ok"],
            scope_checks["timestamp_strictly_monotonic"],
            scope_checks["minimum_trigger_spacing_ok"],
            not clipping_frames,
            not coverage_failures,
            sample_rate_ok,
            record_length_ok,
            sample_spacing_ok,
            interpolation_ratio_ok,
            no_event_errors,
        )
    )
    mapping_status = (
        "scope_valid_awg_execution_log_pending"
        if scope_valid
        else "invalid_scope_qc"
    )
    qc = {
        "scope_checks": scope_checks,
        "scope_valid": scope_valid,
        "mapping_basis": "contract_positional_1based",
        "mapping_integrity_status": mapping_status,
        "important_limitation": (
            "Even 120 monotonic frames cannot exclude a missed legal trigger plus an extra "
            "noise trigger without the AWG execution log. No frame was reclassified from CH3 shape."
        ),
        "no_free_shift": True,
    }
    atomic_write_json(attempt / "qc_report.json", qc)

    manifest = {
        "schema_version": 1,
        "run_id": contract["run_id"],
        "batch_key": batch_key,
        "batch_id": batch["batch_id"],
        "experiment": batch["experiment"],
        "target_layers": batch["target_layers"],
        "created": iso_now(),
        "scope_resource": contract["scope_resource"],
        "effective_scope_config": effective_scope,
        "preamble_common": preamble,
        "expected_frames": len(mapping),
        "reported_acquired_frames": acquisition_status["frames_acquired"],
        "downloaded_frames": int(raw.shape[0]),
        "points_per_frame": int(raw.shape[1]),
        "path_delay": {
            "quantity": "differential_path_delay_ch3_minus_aux",
            "value_ns": path_delay_ns,
            "source": path_delay_source,
            "applied_scope_delay_ns": float(batch["digital_center_ns"]) + path_delay_ns,
        },
        "mapping_basis": "contract_positional_1based",
        "mapping_integrity_status": mapping_status,
        "awg_execution_log_imported": False,
        "no_free_shift": True,
        "event_status": event_status,
        "qc_report": "qc_report.json",
        "frame_index": "frame_index.csv",
        "raw_frames": "raw_frames.npy",
        "files_sha256": {
            "raw_frames.npy": sha256_file(attempt / "raw_frames.npy"),
            "frame_index.csv": sha256_file(attempt / "frame_index.csv"),
            "preamble_common.json": sha256_file(attempt / "preamble_common.json"),
        },
    }
    atomic_write_json(attempt / "capture_manifest.json", manifest)
    atomic_write_json(attempt / "scpi_transcript.json", transcript)
    return {"manifest": manifest, "qc": qc}


def estimate_preview_calibration(
    attempt: Path,
    batch: dict[str, Any],
    mapping: list[dict[str, Any]],
    raw: np.ndarray,
    preamble: dict[str, Any],
    xzero: np.ndarray,
    path_delay_initial_ns: float,
) -> dict[str, Any]:
    if batch["experiment"] != "gatelen":
        raise ValueError("Automatic path preview is intentionally limited to gatelen tfall")
    time_axes, voltage = build_time_and_voltage(raw, preamble, xzero)
    candidates: list[dict[str, Any]] = []
    fig, axis = plt.subplots(figsize=(12, 6), constrained_layout=True)
    for point in batch["points"]:
        indices = [
            index
            for index, row in enumerate(mapping)
            if row["point_id"] == point["point_id"]
        ]
        grid, aligned = common_grid(time_axes[indices], voltage[indices])
        mean = np.mean(aligned, axis=0)
        smooth = np.convolve(mean, np.ones(7) / 7.0, mode="same")
        derivative = np.abs(np.gradient(smooth, grid))
        expected_center = (float(point["target_center_ns"]) + path_delay_initial_ns) * 1e-9
        search_half_width = 8e-9
        mask = np.abs(grid - expected_center) <= search_half_width
        if not np.any(mask):
            raise RuntimeError(f"No preview samples near {point['point_id']} expected tfall")
        indices_in_mask = np.flatnonzero(mask)
        best = indices_in_mask[int(np.argmax(derivative[mask]))]
        measured_time = float(grid[best])
        delay_ns = measured_time * 1e9 - float(point["target_center_ns"])
        candidates.append(
            {
                "point_id": point["point_id"],
                "measured_edge_time_ns": measured_time * 1e9,
                "programmed_tfall_center_ns": float(point["target_center_ns"]),
                "path_delay_candidate_ns": delay_ns,
            }
        )
        axis.plot(grid * 1e9, mean * 1e3, lw=1.1, label=point["point_id"])
        axis.axvline(measured_time * 1e9, color="black", ls=":", lw=0.5)
    delays = np.asarray([item["path_delay_candidate_ns"] for item in candidates])
    median_delay = float(np.median(delays))
    median_absolute_deviation = float(np.median(np.abs(delays - median_delay)))
    baseline = float(np.median(voltage))
    max_deviation = float(np.max(np.abs(voltage - baseline)))
    required_scale = max_deviation / 3.5
    standard_scales = np.asarray([2, 5, 10, 20, 50, 100, 200], dtype=float) * 1e-3
    valid = standard_scales[standard_scales >= required_scale]
    suggested_scale = float(valid[0] if len(valid) else standard_scales[-1])
    axis.set_title("Wide-window path-delay preview; dotted lines are one common-algorithm tfall picks")
    axis.set_xlabel("Time relative to AUX trigger (ns)")
    axis.set_ylabel("CH3 (mV)")
    axis.grid(True)
    axis.legend(fontsize=8, ncol=2)
    fig.savefig(attempt / "path_delay_preview.png", dpi=180)
    plt.close(fig)
    calibration = {
        "schema_version": 1,
        "created": iso_now(),
        "status": "candidate_requires_visual_review",
        "quantity": "differential_path_delay_ch3_minus_aux",
        "path_delay_ns": median_delay,
        "candidate_median_absolute_deviation_ns": median_absolute_deviation,
        "candidate_values": candidates,
        "initial_prior_ns": path_delay_initial_ns,
        "vertical_offset_v": baseline,
        "vertical_scale_v_per_div": suggested_scale,
        "preview_raw_min": int(raw.min()),
        "preview_raw_max": int(raw.max()),
        "warning": (
            "Review path_delay_preview.png before using this value. The same single value must "
            "be used for all formal points; never refit individual frames."
        ),
    }
    atomic_write_json(attempt / "scope_path_calibration.json", calibration)
    return calibration


def acquisition_attempt(
    contract: dict[str, Any],
    batch_key: str,
    *,
    output_root: Path,
    path_delay_ns: float,
    path_delay_source: str,
    span_ns: float,
    vertical_scale_v: float,
    vertical_offset_v: float,
    shots_per_point_override: int | None,
    capture_timeout_s: float,
    category: str,
    preview_calibration: bool,
) -> tuple[Path, dict[str, Any] | None]:
    batch = contract["batches"][batch_key]
    mapping = frame_map_for_batch(batch, shots_per_point_override)
    frame_count = len(mapping)
    attempt = make_attempt_dir(output_root, contract["run_id"], batch["batch_id"], category)
    atomic_write_text(attempt / "CAPTURE_IN_PROGRESS", iso_now() + "\n")
    atomic_write_json(attempt / "contract_snapshot.json", contract)
    requested = {
        "batch_key": batch_key,
        "batch_id": batch["batch_id"],
        "frames": frame_count,
        "shots_per_point": batch["shots_per_point"] if shots_per_point_override is None else shots_per_point_override,
        "path_delay_ns": path_delay_ns,
        "path_delay_source": path_delay_source,
        "span_ns": span_ns,
        "vertical_scale_v_per_div": vertical_scale_v,
        "vertical_offset_v": vertical_offset_v,
    }
    atomic_write_json(attempt / "requested_scope_config.json", requested)
    result: dict[str, Any] | None = None
    with ScopeIO(contract["scope_resource"]) as io:
        event_status: list[dict[str, str]] = []
        try:
            effective = configure_scope(
                io,
                contract,
                batch,
                frame_count=frame_count,
                path_delay_ns=path_delay_ns,
                span_ns=span_ns,
                vertical_scale_v=vertical_scale_v,
                vertical_offset_v=vertical_offset_v,
            )
            atomic_write_json(attempt / "effective_scope_config.json", effective)
            event_status.append(read_event_status(io, "after_configuration"))
            arm_and_wait_for_ready(io, ready_timeout_s=15.0)
            print("\n" + "=" * 76, flush=True)
            print("SCOPE READY", flush=True)
            print(f"batch_id : {batch['batch_id']}", flush=True)
            print(f"frames   : {frame_count}", flush=True)
            print(f"window   : AUX + {effective['requested_scope_delay_s'] * 1e9:.3f} ns center", flush=True)
            print("Now run the matching AWG batch and enter this exact confirmation there:", flush=True)
            print(batch["confirmation"], flush=True)
            print("=" * 76 + "\n", flush=True)

            status = wait_for_frames(io, frame_count, capture_timeout_s)
            event_status.append(read_event_status(io, "after_acquisition"))
            raw, preamble, xzero, timestamps = download_fastframes(io, frame_count)
            event_status.append(read_event_status(io, "after_curve_transfer"))
            result = save_capture(
                attempt,
                contract,
                batch_key,
                batch,
                mapping,
                raw,
                preamble,
                xzero,
                timestamps,
                effective,
                path_delay_ns,
                path_delay_source,
                status,
                event_status,
                io.transcript,
            )
            calibration = None
            if preview_calibration:
                calibration = estimate_preview_calibration(
                    attempt,
                    batch,
                    mapping,
                    raw,
                    preamble,
                    xzero,
                    path_delay_ns,
                )
            completion = {
                "time": iso_now(),
                "scope_qc_valid": result["qc"]["scope_valid"],
                "mapping_integrity_status": result["manifest"]["mapping_integrity_status"],
                "awg_execution_log_still_required": True,
                "calibration": calibration,
            }
            atomic_write_json(attempt / "CAPTURE_COMPLETE.json", completion)
            (attempt / "CAPTURE_IN_PROGRESS").unlink(missing_ok=True)
            return attempt, calibration
        except Exception as exc:
            io.safe_stop()
            failure = {
                "time": iso_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "mapping_integrity_status": "invalid_capture_failure",
            }
            atomic_write_json(attempt / "CAPTURE_INVALID.json", failure)
            atomic_write_json(attempt / "scpi_transcript.json", io.transcript)
            (attempt / "CAPTURE_IN_PROGRESS").unlink(missing_ok=True)
            raise
        finally:
            io.safe_stop()


def calibration_from_file(path: Path) -> tuple[float, float, float, str]:
    value = load_json(path)
    return (
        float(value["path_delay_ns"]),
        float(value["vertical_scale_v_per_div"]),
        float(value["vertical_offset_v"]),
        f"calibration_file:{path.resolve()}",
    )


def print_contract_summary(contract: dict[str, Any]) -> None:
    print(f"run_id: {contract['run_id']}")
    print(
        f"AWG period: {float(contract['awg_period_s']) * 1e6:.6f} us; "
        f"marker rises/period: {contract['marker_rising_edges_per_period']}"
    )
    print(
        "Prior-only path-delay estimate: "
        f"{contract['prior_path_delay']['value_ns']:.3f} ns "
        "(formal capture requires a preview or an explicit value)"
    )
    print("\nBatches")
    for key, batch in contract["batches"].items():
        print(
            f"  {key:14s}  frames={batch['total_frames']:3d}  "
            f"digital_center={batch['digital_center_ns']:.3f} ns  "
            f"span={batch['scope_span_ns']:.1f} ns"
        )


def command_audit(args: argparse.Namespace, contract: dict[str, Any]) -> None:
    print_contract_summary(contract)
    with ScopeIO(contract["scope_resource"]) as io:
        snapshot = query_scope_snapshot(io)
    print("\nScope")
    for key, value in snapshot.items():
        print(f"  {key:28s}: {value}")


def command_preflight(args: argparse.Namespace, contract: dict[str, Any]) -> None:
    batch = contract["batches"][args.batch]
    path_delay_ns = (
        float(args.path_delay_ns)
        if args.path_delay_ns is not None
        else float(contract["prior_path_delay"]["value_ns"])
    )
    with ScopeIO(contract["scope_resource"]) as io:
        try:
            effective = configure_scope(
                io,
                contract,
                batch,
                frame_count=int(batch["total_frames"]),
                path_delay_ns=path_delay_ns,
                span_ns=float(batch["scope_span_ns"]),
                vertical_scale_v=args.vertical_scale_mv * 1e-3,
                vertical_offset_v=args.vertical_offset_v,
            )
            events = read_event_status(io, "preflight")
            print(json.dumps(effective, ensure_ascii=False, indent=2))
            print("\nEvent status")
            print(json.dumps(events, ensure_ascii=False, indent=2))
            print("\nPreflight passed. The scope was not armed and no trigger was consumed.")
        finally:
            io.safe_stop()


def command_smoke_test(args: argparse.Namespace, contract: dict[str, Any]) -> None:
    """Exercise FastFrame transfer with scope-forced triggers, never the AWG."""
    batch = contract["batches"]["gatelen"]
    path_delay_ns = float(contract["prior_path_delay"]["value_ns"])
    defaults = contract["scope_defaults"]
    with ScopeIO(contract["scope_resource"]) as io:
        try:
            configure_scope(
                io,
                contract,
                batch,
                frame_count=3,
                path_delay_ns=path_delay_ns,
                span_ns=40.0,
                vertical_scale_v=float(defaults["safe_vertical_scale_v_per_div"]),
                vertical_offset_v=float(defaults["safe_vertical_offset_v"]),
            )
            arm_and_wait_for_ready(io, ready_timeout_s=15.0)
            for expected in range(1, 4):
                io.write("TRIGGER FORCE")
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if io.query_int("ACQUIRE:NUMFRAMESACQUIRED?") >= expected:
                        break
                    time.sleep(0.02)
                else:
                    raise TimeoutError(f"Forced frame {expected} was not acquired")
                if expected < 3:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if io.query_int("TRIGGER:A:READY?") == 1:
                            break
                        time.sleep(0.02)
                    else:
                        raise TimeoutError("Scope did not re-arm between forced frames")
            status = wait_for_frames(io, 3, capture_timeout_s=10.0)
            raw, preamble, xzero, timestamps = download_fastframes(io, 3)
            events = read_event_status(io, "smoke_test")
            print("FastFrame smoke test passed")
            print(f"  acquired/downloaded : {status['frames_acquired']}/{raw.shape[0]}")
            print(f"  raw shape           : {raw.shape}")
            print(f"  XINCR               : {preamble['XINCR'] * 1e12:.6f} ps")
            print(f"  XZERO per frame     : {xzero.tolist()}")
            print(f"  timestamps          : {timestamps}")
            print(f"  event status        : {events}")
        finally:
            io.safe_stop()


def command_preview(args: argparse.Namespace, contract: dict[str, Any]) -> None:
    batch = contract["batches"][args.batch]
    if batch["experiment"] != "gatelen":
        raise SystemExit("Path-delay preview must use the gatelen tfall batch")
    initial = (
        float(args.initial_path_delay_ns)
        if args.initial_path_delay_ns is not None
        else float(contract["prior_path_delay"]["value_ns"])
    )
    defaults = contract["scope_defaults"]
    attempt, calibration = acquisition_attempt(
        contract,
        args.batch,
        output_root=args.output_root,
        path_delay_ns=initial,
        path_delay_source="prior_staircase_initial_guess_for_preview",
        span_ns=args.span_ns,
        vertical_scale_v=args.vertical_scale_mv * 1e-3,
        vertical_offset_v=args.vertical_offset_v,
        shots_per_point_override=args.shots_per_point,
        capture_timeout_s=args.capture_timeout_s,
        category="calibration",
        preview_calibration=True,
    )
    assert calibration is not None
    print("\nPreview saved:", attempt)
    print(f"Candidate fixed path delay: {calibration['path_delay_ns']:.6f} ns")
    print(
        "Candidate spread (median absolute deviation): "
        f"{calibration['candidate_median_absolute_deviation_ns']:.6f} ns"
    )
    print("Review:", attempt / "path_delay_preview.png")
    print("Calibration file:", attempt / "scope_path_calibration.json")


def command_capture(args: argparse.Namespace, contract: dict[str, Any]) -> None:
    defaults = contract["scope_defaults"]
    if args.calibration_file is not None:
        path_delay_ns, vertical_scale_v, vertical_offset_v, source = calibration_from_file(
            args.calibration_file
        )
        if args.path_delay_ns is not None:
            raise SystemExit("Use either --calibration-file or --path-delay-ns, not both")
    elif args.path_delay_ns is not None:
        path_delay_ns = float(args.path_delay_ns)
        vertical_scale_v = args.vertical_scale_mv * 1e-3
        vertical_offset_v = args.vertical_offset_v
        source = "explicit_command_line_value"
    else:
        raise SystemExit(
            "Formal capture refuses an unverified path delay. Run preview first, then pass "
            "--calibration-file, or explicitly pass --path-delay-ns."
        )
    attempt, _ = acquisition_attempt(
        contract,
        args.batch,
        output_root=args.output_root,
        path_delay_ns=path_delay_ns,
        path_delay_source=source,
        span_ns=float(contract["batches"][args.batch]["scope_span_ns"]),
        vertical_scale_v=vertical_scale_v,
        vertical_offset_v=vertical_offset_v,
        shots_per_point_override=None,
        capture_timeout_s=args.capture_timeout_s,
        category="batches",
        preview_calibration=False,
    )
    print("\nCapture saved:", attempt)
    print("The positional frame map remains provisional until the AWG execution log is checked.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DPO70404C FastFrame capture for the formal M=20 longitudinal scans"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("audit", help="Read-only contract and current-scope audit")

    preflight = subparsers.add_parser(
        "preflight", help="Configure/query one batch without arming or consuming a trigger"
    )
    preflight.add_argument(
        "--batch",
        required=True,
        choices=("gatelen", "timinglag_l1", "timinglag_l10", "timinglag_l20"),
    )
    preflight.add_argument("--path-delay-ns", type=float)
    preflight.add_argument("--vertical-scale-mv", type=float, default=20.0)
    preflight.add_argument("--vertical-offset-v", type=float, default=0.2432)

    subparsers.add_parser(
        "smoke-test",
        help="Three scope-forced frames used only to test FastFrame transfer; no AWG needed",
    )

    preview = subparsers.add_parser(
        "preview", help="Wide-window pilot used once to estimate the common AUX-to-CH3 delay"
    )
    preview.add_argument("--batch", choices=("gatelen",), default="gatelen")
    preview.add_argument("--shots-per-point", type=int, default=1)
    preview.add_argument("--span-ns", type=float, default=80.0)
    preview.add_argument("--initial-path-delay-ns", type=float)
    preview.add_argument("--vertical-scale-mv", type=float, default=20.0)
    preview.add_argument("--vertical-offset-v", type=float, default=0.2432)
    preview.add_argument("--capture-timeout-s", type=float, default=900.0)

    capture = subparsers.add_parser("capture", help="Capture one formal 120-frame batch")
    capture.add_argument(
        "--batch",
        required=True,
        choices=("gatelen", "timinglag_l1", "timinglag_l10", "timinglag_l20"),
    )
    capture.add_argument("--calibration-file", type=Path)
    capture.add_argument("--path-delay-ns", type=float)
    capture.add_argument("--vertical-scale-mv", type=float, default=20.0)
    capture.add_argument("--vertical-offset-v", type=float, default=0.2432)
    capture.add_argument("--capture-timeout-s", type=float, default=1800.0)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.config = args.config.resolve()
    args.output_root = args.output_root.resolve()
    contract = load_json(args.config)
    validate_contract(contract)
    if args.command == "audit":
        command_audit(args, contract)
    elif args.command == "preflight":
        command_preflight(args, contract)
    elif args.command == "smoke-test":
        command_smoke_test(args, contract)
    elif args.command == "preview":
        if args.shots_per_point < 1:
            parser.error("--shots-per-point must be positive")
        command_preview(args, contract)
    elif args.command == "capture":
        command_capture(args, contract)


if __name__ == "__main__":
    main()
