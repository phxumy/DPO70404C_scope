from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


BASE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "scope_runs"
    / "timing-response-shots-20260817_120443-6d3f0901"
    / "precision_batches"
)
FLAT_LO_NS = 7248.0
FLAT_HI_NS = 7258.0


def latest_complete_attempt(batch_dir: Path) -> Path | None:
    attempts = sorted(
        candidate
        for candidate in batch_dir.glob("attempt_*")
        if (candidate / "CAPTURE_COMPLETE.json").exists()
    )
    return attempts[-1] if attempts else None


def load_attempt_overrides(run_root: Path) -> dict[str, str]:
    path = run_root / "attempt_selection.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def select_attempt(batch_dir: Path, overrides: dict[str, str]) -> Path | None:
    latest = latest_complete_attempt(batch_dir)
    if latest is None:
        return None
    manifest = json.loads(
        (latest / "capture_manifest.json").read_text(encoding="utf-8")
    )
    chosen = overrides.get(manifest["batch_key"])
    if chosen:
        candidate = batch_dir / chosen
        if (candidate / "CAPTURE_COMPLETE.json").exists():
            return candidate
    return latest


def flat_slope(npz_path: Path) -> float | None:
    with np.load(npz_path) as data:
        time_ns = np.asarray(data["common_time_aux_relative_s"]) * 1e9
        voltage_mv = np.asarray(data["mean_voltage"]) * 1e3
    mask = (time_ns >= FLAT_LO_NS) & (time_ns <= FLAT_HI_NS)
    if int(np.count_nonzero(mask)) < 20:
        return None
    slope = float(np.polyfit(time_ns[mask], voltage_mv[mask], 1)[0])
    return slope


def main() -> None:
    rows = []
    overrides = load_attempt_overrides(BASE.parent)
    for batch_dir in sorted(BASE.glob("*")):
        attempt = select_attempt(batch_dir, overrides)
        if attempt is None:
            continue
        manifest = json.loads(
            (attempt / "capture_manifest.json").read_text(encoding="utf-8")
        )
        batch_key = manifest["batch_key"]
        derived = attempt / "derived"
        zero_files = sorted(derived.glob("*0p0ns*.npz"))
        if not zero_files:
            print(f"{batch_key:24s} no zero npz")
            continue
        all_files = sorted(derived.glob("*.npz"))
        slopes = [flat_slope(path) for path in all_files]
        slopes = [value for value in slopes if value is not None]
        zero_slope = flat_slope(zero_files[0])
        print(
            f"{batch_key:24s} zero_slope={zero_slope:+.4f} mV/ns "
            f"median_abs_slope={np.median(np.abs(slopes)):.4f} "
            f"max_abs_slope={np.max(np.abs(slopes)):.4f} mV/ns (n={len(slopes)})"
        )
        complete = attempt / "CAPTURE_COMPLETE.json"
        rows.append(
            (
                complete.stat().st_mtime,
                batch_key,
                zero_slope if zero_slope is not None else float("nan"),
            )
        )

    rows.sort()
    times = np.asarray([row[0] for row in rows])
    times_h = (times - times.min()) / 3600.0
    slopes = np.asarray([row[2] for row in rows])
    labels = [row[1].replace("timing_response_", "") for row in rows]
    fig, axis = plt.subplots(figsize=(9, 5), constrained_layout=True)
    axis.plot(times_h, slopes, "o-")
    for x, y, label in zip(times_h, slopes, labels):
        axis.annotate(label, (x, y), textcoords="offset points", xytext=(0, 8), fontsize=8)
    axis.set_xlabel("hours since first batch")
    axis.set_ylabel("flat-top slope of zero point (mV/ns)")
    axis.set_title("flat-top droop vs acquisition time")
    axis.grid(True, alpha=0.3)
    fig.savefig(BASE.parent / "shots_scaling" / "flat_slope_drift.png", dpi=180)
    plt.close(fig)
    print(f"saved: {BASE.parent / 'shots_scaling' / 'flat_slope_drift.png'}")


if __name__ == "__main__":
    main()
