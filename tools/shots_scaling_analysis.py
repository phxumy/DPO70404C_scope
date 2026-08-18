from __future__ import annotations

import argparse
import csv
import json
import math
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
    parser = argparse.ArgumentParser(
        description="Compare single-digit-ps timing precision across shot counts"
    )
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--point-filters",
        nargs="*",
        default=[],
        help="restrict to point_id substrings used in every batch",
    )
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_attempt_overrides(run_root)

    per_shots: dict[int, dict[str, object]] = {}
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
        batch = contract["batches"][manifest["batch_key"]]
        shots = int(batch["shots_per_point"])
        metrics_path = attempt / "precision_analysis" / "point_metrics.csv"
        if not metrics_path.exists():
            continue
        with metrics_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if args.point_filters:
            rows = [
                row
                for row in rows
                if any(part in row["point_id"] for part in args.point_filters)
            ]
        half_widths = [
            (float(row["time_shift_ci95_high_ps"]) - float(row["time_shift_ci95_low_ps"]))
            / 2.0
            for row in rows
        ]
        single_shot_sd = [
            float(row["shot_time_shift_sd_ps"])
            for row in rows
            if row["shot_time_shift_sd_ps"]
        ]
        if not half_widths:
            continue
        bucket = per_shots.setdefault(
            shots, {"half_widths": [], "shot_sd": [], "n_points": 0, "attempts": []}
        )
        assert isinstance(bucket, dict)
        bucket["half_widths"].extend(half_widths)
        bucket["shot_sd"].extend(single_shot_sd)
        bucket["n_points"] += len(rows)
        bucket["attempts"].append(str(attempt))

    entries: list[dict[str, object]] = []
    for shots in sorted(per_shots):
        bucket = per_shots[shots]
        half_widths = bucket["half_widths"]
        shot_sd = bucket["shot_sd"]
        entries.append(
            {
                "shots": shots,
                "median_ci_half_width_ps": float(np.median(half_widths)),
                "median_shot_sd_ps": float(np.median(shot_sd)) if shot_sd else None,
                "n_points": int(bucket["n_points"]),
                "attempts": list(bucket["attempts"]),
            }
        )
    if not entries:
        raise SystemExit("No completed timing_response batches found under the run root")
    entries.sort(key=lambda item: int(item["shots"]))

    shots = np.asarray([float(item["shots"]) for item in entries])
    half_width = np.asarray([float(item["median_ci_half_width_ps"]) for item in entries])

    slope, intercept = np.polyfit(np.log(shots), np.log(half_width), 1)
    alpha = -float(slope)
    scale = math.exp(float(intercept))
    predicted_shots_for_1ps = int(round((scale / 1.0) ** (1.0 / alpha))) if alpha > 0 else None
    predicted_shots_for_2ps = int(round((scale / 2.0) ** (1.0 / alpha))) if alpha > 0 else None

    fig, axis = plt.subplots(figsize=(9, 6), constrained_layout=True)
    axis.loglog(shots, half_width, "o-", label="measured")
    fit_grid = np.geomspace(shots.min(), shots.max(), 100)
    axis.loglog(
        fit_grid, scale * fit_grid ** (-alpha), "--",
        label=f"fit: CI = {scale:.1f} * N^(-{alpha:.3f})",
    )
    axis.set_xlabel("shots per point")
    axis.set_ylabel("median 95% CI half-width (ps)")
    axis.set_title("timing precision vs shots (single-digit ps scan)")
    axis.grid(True, which="both", alpha=0.3)
    axis.legend(fontsize=8)
    fig.savefig(output_dir / "shots_scaling.png", dpi=180)
    plt.close(fig)

    report = {
        "alpha": alpha,
        "scale_ps": scale,
        "interpretation": (
            "alpha ~ 0.5 means shot-noise dominated (averaging keeps helping); "
            "alpha clearly below 0.5 means correlated jitter/drift dominates and more shots "
            "eventually stop helping."
        ),
        "predicted_shots_for_1ps_ci": predicted_shots_for_1ps,
        "predicted_shots_for_2ps_ci": predicted_shots_for_2ps,
        "entries": entries,
    }
    (output_dir / "shots_scaling.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"saved: {output_dir / 'shots_scaling.png'}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
