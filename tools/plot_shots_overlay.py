from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_attempt_overrides(run_root)

    per_shots: dict[int, list[dict[str, str]]] = {}
    for batch_dir in sorted((run_root / "precision_batches").glob("*")):
        attempt = select_attempt(batch_dir, overrides)
        if attempt is None:
            continue
        manifest = json.loads(
            (attempt / "capture_manifest.json").read_text(encoding="utf-8")
        )
        contract = json.loads(
            (attempt / "contract_snapshot.json").read_text(encoding="utf-8")
        )
        shots = int(contract["batches"][manifest["batch_key"]]["shots_per_point"])
        metrics_path = attempt / "precision_analysis" / "point_metrics.csv"
        if not metrics_path.exists():
            continue
        with metrics_path.open("r", encoding="utf-8", newline="") as handle:
            per_shots.setdefault(shots, []).extend(list(csv.DictReader(handle)))

    colors = {20: "C0", 100: "C1", 500: "C2", 1000: "C3"}
    fig, axis = plt.subplots(figsize=(10, 7), constrained_layout=True)
    for shots in sorted(per_shots):
        rows = per_shots[shots]
        realized = np.asarray([float(row["realized_delta"]) for row in rows]) * 1e3
        measured = np.asarray([float(row["mean_time_shift_ps"]) for row in rows])
        ci_low = np.asarray([float(row["time_shift_ci95_low_ps"]) for row in rows])
        ci_high = np.asarray([float(row["time_shift_ci95_high_ps"]) for row in rows])
        yerr = np.clip(np.vstack([measured - ci_low, ci_high - measured]), 0.0, None)
        order = np.argsort(realized)
        axis.errorbar(
            realized[order], measured[order], yerr=yerr[:, order],
            fmt="o", ms=3, capsize=1.5, lw=0.7, color=colors.get(shots, "gray"),
            label=f"{shots} shots",
        )
    span = 25
    axis.plot([-span, span], [-span, span], "--", color="gray", lw=1.2, label="ideal 1:1")
    axis.set_xlabel("Realized timing command (ps)")
    axis.set_ylabel("Measured fixed-template shift (ps)")
    axis.set_title("timing_response ±1…±20 ps at 20/100/500/1000 shots")
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8)
    fig.savefig(output_dir / "shots_response_overlay.png", dpi=180)
    plt.close(fig)
    print(f"saved: {output_dir / 'shots_response_overlay.png'}")


if __name__ == "__main__":
    main()
