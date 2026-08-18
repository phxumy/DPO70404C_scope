from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main() -> None:
    attempt = Path(sys.argv[1]).resolve()
    analysis_dir = attempt / "precision_analysis"
    with (analysis_dir / "point_metrics.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = json.loads(
        (analysis_dir / "precision_analysis.json").read_text(encoding="utf-8")
    )

    realized = np.asarray([float(row["realized_delta"]) for row in rows]) * 1e3
    measured = np.asarray([float(row["mean_time_shift_ps"]) for row in rows])
    ci_low = np.asarray([float(row["time_shift_ci95_low_ps"]) for row in rows])
    ci_high = np.asarray([float(row["time_shift_ci95_high_ps"]) for row in rows])
    yerr = np.clip(np.vstack([measured - ci_low, ci_high - measured]), 0.0, None)

    slope = float(summary["global_response"]["slope"])
    intercept_ps = float(summary["global_response"]["intercept"]) * 1e3
    order = np.argsort(realized)
    fitted = intercept_ps + slope * realized
    residual = measured - fitted
    residual_rms = float(np.sqrt(np.mean(residual**2)))

    fig, axes = plt.subplots(
        2,
        1,
        figsize=(10, 9),
        gridspec_kw={"height_ratios": [3, 1]},
        constrained_layout=True,
    )
    axes[0].errorbar(
        realized, measured, yerr=yerr, fmt="o", ms=3, capsize=1.5, lw=0.8,
        color="tab:blue", zorder=3,
    )
    axes[0].plot(
        realized[order], fitted[order], "-", color="tab:red", lw=1.5,
        label=f"linear fit, slope = {slope:.4f}",
    )
    axes[0].plot(
        realized[order], realized[order], "--", color="gray", lw=1.2,
        label="ideal 1:1",
    )
    axes[0].set_xlabel("Realized timing command (ps)")
    axes[0].set_ylabel("Measured fixed-template shift (ps)")
    axes[0].set_title(
        "timing_response: 0 / ±2…±120 ps, 140 shots/point, 95% block-bootstrap CI"
    )
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(fontsize=8)

    axes[1].axhline(0, color="black", lw=0.8)
    axes[1].plot(realized, residual, "o", ms=3, color="tab:blue")
    axes[1].set_xlabel("Realized timing command (ps)")
    axes[1].set_ylabel("Residual (ps)")
    axes[1].set_title(f"Residual vs linear fit; RMS = {residual_rms:.2f} ps")
    axes[1].grid(True, alpha=0.3)

    output = analysis_dir / "timing_response_curve.png"
    fig.savefig(output, dpi=180)
    plt.close(fig)
    print(f"saved: {output}")
    print(
        f"slope={slope:.6f} intercept_ps={intercept_ps:.3f} "
        f"residual_rms_ps={residual_rms:.2f} n_points={len(rows)}"
    )


if __name__ == "__main__":
    main()
