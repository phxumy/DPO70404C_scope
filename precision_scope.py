from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import formal_longitudinal_scope as formal


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "precision_scope_contract.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "scope_runs"
ALLOWED_EXPERIMENTS = {"timing_precision", "amplitude_precision", "timing_response"}
TIMING_EXPERIMENTS = {"timing_precision", "timing_response"}
REQUIRED_TIMING_MAGNITUDES_NS = {
    Decimal("0"),
    Decimal("0.001"),
    Decimal("0.005"),
    Decimal("0.01"),
    Decimal("0.05"),
}
REQUIRED_AMPLITUDE_MAGNITUDES = {
    Decimal("0"),
    Decimal("0.0001"),
    Decimal("0.0005"),
    Decimal("0.001"),
    Decimal("0.005"),
    Decimal("0.01"),
}


def decimal_from(point: dict[str, Any], keys: tuple[str, ...]) -> Decimal:
    for key in keys:
        if key in point:
            value = point[key]
            if not isinstance(value, str):
                raise ValueError(
                    f"{point.get('point_id', '<unknown>')}.{key} must be a JSON string"
                )
            return Decimal(value)
    raise ValueError(
        f"{point.get('point_id', '<unknown>')} is missing one of {', '.join(keys)}"
    )


def requested_coordinate(point: dict[str, Any], experiment: str) -> Decimal:
    if experiment in TIMING_EXPERIMENTS:
        return decimal_from(
            point,
            (
                "requested_delta_ns",
                "requested_timing_lag_offset_ns",
            ),
        )
    return decimal_from(
        point,
        (
            "requested_amp_delta",
            "signed_requested_delta",
        ),
    )


def realized_coordinate(point: dict[str, Any], experiment: str) -> Decimal:
    if experiment in TIMING_EXPERIMENTS:
        return decimal_from(
            point,
            (
                "realized_delta_ns",
                "realized_timing_lag_offset_ns",
                "actual_digital_offset_ns",
            ),
        )
    return decimal_from(
        point,
        (
            "realized_amp_delta",
            "actual_amp_delta",
            "actual_quantized_amp_delta",
        ),
    )


def validate_precision_contract(contract: dict[str, Any]) -> None:
    if contract.get("template_only"):
        raise ValueError(
            "This is a template contract. Replace it with the AWG dry-run handoff before arming."
        )
    if contract.get("contract_status") != "ready_for_scope_capture":
        raise ValueError("contract_status must be 'ready_for_scope_capture'")
    formal.validate_contract(contract)
    seen_batch_ids: set[str] = set()
    experiments_seen: set[str] = set()
    requested_union: dict[str, set[Decimal]] = {}
    for batch_key, batch in contract["batches"].items():
        experiment = str(batch.get("experiment", "")).lower()
        if experiment not in ALLOWED_EXPERIMENTS:
            raise ValueError(f"{batch_key}: unsupported experiment {experiment!r}")
        experiments_seen.add(experiment)
        batch_id = str(batch["batch_id"])
        if batch_id in seen_batch_ids:
            raise ValueError(f"Duplicate batch_id: {batch_id}")
        seen_batch_ids.add(batch_id)
        if not batch.get("marker_fixed_across_points", False):
            raise ValueError(f"{batch_key}: marker_fixed_across_points must be true")
        if int(batch.get("warmup_shots", 0)) != 0:
            raise ValueError(f"{batch_key}: warmup_shots must be zero for positional mapping")
        analysis = batch.get("analysis")
        if not isinstance(analysis, dict):
            raise ValueError(f"{batch_key}: missing analysis object")
        reference_id = analysis.get("reference_point_id")
        point_ids = [str(point["point_id"]) for point in batch["points"]]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError(f"{batch_key}: point_id values must be unique")
        if reference_id not in point_ids:
            raise ValueError(f"{batch_key}: reference_point_id is not a point")
        roi = analysis.get("active_roi_digital_ns")
        if not isinstance(roi, list) or len(roi) != 2 or float(roi[1]) <= float(roi[0]):
            raise ValueError(f"{batch_key}: invalid active_roi_digital_ns")
        coverage_left, coverage_right = map(float, batch["digital_coverage_ns"])
        if float(roi[0]) < coverage_left or float(roi[1]) > coverage_right:
            raise ValueError(f"{batch_key}: active ROI lies outside digital coverage")
        requested_magnitudes: set[Decimal] = set()
        reference_count = 0
        marker_hashes: set[str] = set()
        sram_hashes: dict[Decimal, set[str]] = {}
        for point in batch["points"]:
            request = requested_coordinate(point, experiment)
            realized = realized_coordinate(point, experiment)
            requested_magnitudes.add(abs(request))
            if request == 0:
                reference_count += 1
            if int(point.get("marker_rising_edges_per_period", 1)) != 1:
                raise ValueError(f"{point['point_id']}: expected exactly one marker rise")
            if "marker_sha256" in point:
                marker_hashes.add(str(point["marker_sha256"]))
            sram_hash = str(point.get("final_sram_sha256", ""))
            if not sram_hash:
                raise ValueError(f"{point['point_id']}: final_sram_sha256 is required")
            sram_hashes.setdefault(realized, set()).add(sram_hash)
        if reference_count != 1:
            raise ValueError(f"{batch_key}: exactly one zero-delta reference point is required")
        if len(marker_hashes) > 1:
            raise ValueError(f"{batch_key}: marker arrays differ across points")
        declared = {
            Decimal(str(value))
            for value in batch.get("required_requested_magnitudes", [])
        }
        if declared and declared != requested_magnitudes:
            raise ValueError(
                f"{batch_key}: required_requested_magnitudes must match this batch's points"
            )
        requested_union.setdefault(experiment, set()).update(requested_magnitudes)
    for experiment, required in {
        "timing_precision": REQUIRED_TIMING_MAGNITUDES_NS,
        "amplitude_precision": REQUIRED_AMPLITUDE_MAGNITUDES,
    }.items():
        if experiment not in experiments_seen:
            continue
        if not required.issubset(requested_union.get(experiment, set())):
            missing = sorted(required - requested_union.get(experiment, set()))
            raise ValueError(f"{experiment}: missing requested magnitudes {missing}")


def arm_precision_batch(
    contract_path: Path,
    batch_key: str,
    calibration_file: Path,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    capture_timeout_s: float = 1800.0,
    vertical_scale_v: float | None = None,
    vertical_offset_v: float | None = None,
) -> Path:
    """Configure one FastFrame batch, arm it once, wait, download, and save every frame."""
    contract = formal.load_json(contract_path)
    validate_precision_contract(contract)
    if batch_key not in contract["batches"]:
        raise KeyError(f"Unknown batch {batch_key!r}; choose from {sorted(contract['batches'])}")
    path_delay_ns, calibrated_scale, calibrated_offset, source = formal.calibration_from_file(
        calibration_file
    )
    effective_scale = calibrated_scale if vertical_scale_v is None else vertical_scale_v
    effective_offset = calibrated_offset if vertical_offset_v is None else vertical_offset_v
    batch = contract["batches"][batch_key]
    attempt, _ = formal.acquisition_attempt(
        contract,
        batch_key,
        output_root=output_root,
        path_delay_ns=path_delay_ns,
        path_delay_source=source,
        span_ns=float(batch["scope_span_ns"]),
        vertical_scale_v=effective_scale,
        vertical_offset_v=effective_offset,
        shots_per_point_override=None,
        capture_timeout_s=capture_timeout_s,
        category="precision_batches",
        preview_calibration=False,
    )
    return attempt


def moving_average(values: np.ndarray, points: int) -> np.ndarray:
    points = max(1, int(points))
    if points % 2 == 0:
        points += 1
    if points == 1:
        return values.copy()
    padding = points // 2
    padded = np.pad(values, padding, mode="edge")
    return np.convolve(padded, np.ones(points) / points, mode="valid")


def fit_template_coefficients(
    trace: np.ndarray,
    reference: np.ndarray,
    derivative_v_per_ns: np.ndarray,
    mask: np.ndarray,
) -> dict[str, float]:
    delta = trace[mask] - reference[mask]
    waveform = reference[mask] - float(np.mean(reference[mask]))
    derivative = derivative_v_per_ns[mask]
    design = np.column_stack((-derivative, waveform, np.ones_like(waveform)))
    coefficient, _, _, _ = np.linalg.lstsq(design, delta, rcond=None)
    predicted = design @ coefficient
    residual = delta - predicted
    normalized = design.copy()
    for column in range(normalized.shape[1]):
        norm = float(np.linalg.norm(normalized[:, column]))
        if norm > 0:
            normalized[:, column] /= norm
    return {
        "time_shift_ns": float(coefficient[0]),
        "amplitude_gain_fraction": float(coefficient[1]),
        "offset_v": float(coefficient[2]),
        "residual_rms_v": float(np.sqrt(np.mean(residual**2))),
        "design_condition_number": float(np.linalg.cond(normalized)),
        "template_derivative_correlation": float(
            np.corrcoef(waveform, derivative)[0, 1]
        ),
    }


def circular_block_indices(
    count: int, block_length: int, rng: np.random.Generator
) -> np.ndarray:
    if count < 1:
        raise ValueError("Cannot resample an empty shot group")
    block_length = min(max(1, block_length), count)
    blocks = int(math.ceil(count / block_length))
    starts = rng.integers(0, count, size=blocks)
    result = np.concatenate(
        [(start + np.arange(block_length)) % count for start in starts]
    )
    return result[:count]


def _point_coordinate_fields(
    point: dict[str, Any], experiment: str
) -> tuple[str, str, str]:
    requested = str(requested_coordinate(point, experiment))
    realized = str(realized_coordinate(point, experiment))
    if experiment in TIMING_EXPERIMENTS:
        unit = "ns"
    else:
        unit = str(point.get("amp_unit", point.get("parameter_unit", "AWG_amp_unit")))
    return requested, realized, unit


def analyze_precision_attempt(
    attempt: Path,
    *,
    bootstrap_replicates: int | None = None,
    random_seed: int = 20260816,
) -> dict[str, Any]:
    """Analyze a completed batch without searching or shifting any waveform in time."""
    attempt = attempt.resolve()
    if not (attempt / "CAPTURE_COMPLETE.json").exists():
        raise ValueError(f"No CAPTURE_COMPLETE.json in {attempt}")
    contract = formal.load_json(attempt / "contract_snapshot.json")
    validate_precision_contract(contract)
    manifest = formal.load_json(attempt / "capture_manifest.json")
    qc = formal.load_json(attempt / "qc_report.json")
    if not qc.get("scope_valid", False):
        raise ValueError("Scope QC is not valid; analysis is intentionally blocked")
    batch_key = str(manifest["batch_key"])
    batch = contract["batches"][batch_key]
    experiment = str(batch["experiment"]).lower()
    mapping = formal.frame_map_for_batch(batch)
    raw = np.load(attempt / "raw_frames.npy", allow_pickle=False)
    xzero = np.load(attempt / "frame_xzero_s.npy", allow_pickle=False)
    preamble = formal.load_json(attempt / "preamble_common.json")
    time_axes, voltage = formal.build_time_and_voltage(raw, preamble, xzero)
    grid, traces = formal.common_grid(time_axes, voltage)
    path_delay_ns = float(manifest["path_delay"]["value_ns"])
    analysis = batch["analysis"]
    roi_digital = list(map(float, analysis["active_roi_digital_ns"]))
    roi_scope_ns = np.asarray(roi_digital) + path_delay_ns
    grid_ns = grid * 1e9
    mask = (grid_ns >= roi_scope_ns[0]) & (grid_ns <= roi_scope_ns[1])
    if int(np.count_nonzero(mask)) < 20:
        raise ValueError("Active ROI has fewer than 20 scope samples")

    point_indices: dict[str, list[int]] = {}
    point_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(mapping):
        point_indices.setdefault(row["point_id"], []).append(index)
        point_by_id[row["point_id"]] = row["point"]
    reference_id = str(analysis["reference_point_id"])
    reference_indices = point_indices[reference_id]
    reference_mean = np.mean(traces[reference_indices], axis=0)
    smoothing_points = int(analysis.get("derivative_smoothing_points", 7))
    reference_smooth = moving_average(reference_mean, smoothing_points)
    derivative = np.gradient(reference_smooth, grid_ns)
    reference_pp_v = float(np.ptp(reference_mean[mask]))

    point_rows: list[dict[str, Any]] = []
    individual_metrics: dict[str, np.ndarray] = {}
    point_means: dict[str, np.ndarray] = {}
    for point in batch["points"]:
        point_id = str(point["point_id"])
        indices = point_indices[point_id]
        point_traces = traces[indices]
        point_mean = np.mean(point_traces, axis=0)
        point_means[point_id] = point_mean
        mean_fit = fit_template_coefficients(
            point_mean, reference_smooth, derivative, mask
        )
        shot_fits = [
            fit_template_coefficients(trace, reference_smooth, derivative, mask)
            for trace in point_traces
        ]
        metric_array = np.asarray(
            [
                [
                    item["time_shift_ns"],
                    item["amplitude_gain_fraction"],
                    item["offset_v"],
                    item["residual_rms_v"],
                ]
                for item in shot_fits
            ],
            dtype=float,
        )
        individual_metrics[point_id] = metric_array
        requested, realized, unit = _point_coordinate_fields(point, experiment)
        point_rows.append(
            {
                "point_id": point_id,
                "frames": len(indices),
                "requested_delta": requested,
                "realized_delta": realized,
                "parameter_unit": unit,
                "mean_time_shift_ps": mean_fit["time_shift_ns"] * 1e3,
                "shot_time_shift_sd_ps": float(np.std(metric_array[:, 0], ddof=1)) * 1e3,
                "mean_amplitude_gain_fraction": mean_fit["amplitude_gain_fraction"],
                "mean_amplitude_change_mV_pp": (
                    mean_fit["amplitude_gain_fraction"] * reference_pp_v * 1e3
                ),
                "shot_amplitude_gain_sd": float(np.std(metric_array[:, 1], ddof=1)),
                "mean_offset_change_mv": mean_fit["offset_v"] * 1e3,
                "mean_fit_residual_rms_mv": mean_fit["residual_rms_v"] * 1e3,
                "design_condition_number": mean_fit["design_condition_number"],
                "template_derivative_correlation": mean_fit[
                    "template_derivative_correlation"
                ],
                "final_sram_sha256": point["final_sram_sha256"],
            }
        )

    replicates = int(
        bootstrap_replicates
        if bootstrap_replicates is not None
        else analysis.get("bootstrap_replicates", 2000)
    )
    if replicates < 100:
        raise ValueError("At least 100 bootstrap replicates are required")
    block_length = int(analysis.get("bootstrap_block_shots", 5))
    rng = np.random.default_rng(random_seed)
    bootstrap: dict[str, np.ndarray] = {
        point_id: np.empty((replicates, 4), dtype=float) for point_id in point_indices
    }
    reference_traces = traces[reference_indices]
    for replicate in range(replicates):
        ref_draw_a = circular_block_indices(len(reference_indices), block_length, rng)
        ref_curve = np.mean(reference_traces[ref_draw_a], axis=0)
        for point_id, indices in point_indices.items():
            group = traces[indices]
            draw = circular_block_indices(len(indices), block_length, rng)
            if point_id == reference_id:
                # Two independent draws estimate the zero-reference noise floor.
                ref_draw_b = circular_block_indices(len(reference_indices), block_length, rng)
                curve = np.mean(reference_traces[ref_draw_b], axis=0)
            else:
                curve = np.mean(group[draw], axis=0)
            fit = fit_template_coefficients(curve - ref_curve + reference_smooth,
                                            reference_smooth, derivative, mask)
            bootstrap[point_id][replicate] = (
                fit["time_shift_ns"],
                fit["amplitude_gain_fraction"],
                fit["offset_v"],
                fit["residual_rms_v"],
            )

    for row in point_rows:
        distribution = bootstrap[row["point_id"]]
        row["time_shift_ci95_low_ps"] = float(np.percentile(distribution[:, 0], 2.5)) * 1e3
        row["time_shift_ci95_high_ps"] = float(np.percentile(distribution[:, 0], 97.5)) * 1e3
        row["amplitude_gain_ci95_low"] = float(np.percentile(distribution[:, 1], 2.5))
        row["amplitude_gain_ci95_high"] = float(np.percentile(distribution[:, 1], 97.5))
        row["bootstrap_median_time_shift_ps"] = float(np.median(distribution[:, 0])) * 1e3
        row["bootstrap_median_amplitude_gain_fraction"] = float(
            np.median(distribution[:, 1])
        )

    metric_column = 0 if experiment in TIMING_EXPERIMENTS else 1
    coordinate = np.asarray(
        [float(realized_coordinate(point, experiment)) for point in batch["points"]]
    )
    measured = np.asarray(
        [
            row["bootstrap_median_time_shift_ps"] / 1e3
            if experiment in TIMING_EXPERIMENTS
            else row["bootstrap_median_amplitude_gain_fraction"]
            for row in point_rows
        ]
    )
    response_fit_available = len(np.unique(coordinate)) >= 2
    if response_fit_available:
        slope, intercept = np.polyfit(coordinate, measured, 1)
        fitted = intercept + slope * coordinate
    else:
        slope = None
        intercept = float(np.mean(measured))
        fitted = np.full_like(measured, intercept)
    linear_residual = measured - fitted

    quantization_groups: dict[str, list[dict[str, str]]] = {}
    for point in batch["points"]:
        quantization_groups.setdefault(str(point["final_sram_sha256"]), []).append(
            {
                "point_id": str(point["point_id"]),
                "requested_delta": str(requested_coordinate(point, experiment)),
                "realized_delta": str(realized_coordinate(point, experiment)),
            }
        )

    by_requested = {
        requested_coordinate(point, experiment): str(point["point_id"])
        for point in batch["points"]
    }
    resolution_rows: list[dict[str, Any]] = []
    for magnitude in sorted({abs(value) for value in by_requested if value != 0}):
        positive_id = by_requested.get(magnitude)
        negative_id = by_requested.get(-magnitude)
        if positive_id is not None and negative_id is not None:
            distribution = 0.5 * (
                bootstrap[positive_id][:, metric_column]
                - bootstrap[negative_id][:, metric_column]
            )
            estimate = 0.5 * (
                measured[[point["point_id"] for point in batch["points"]].index(positive_id)]
                - measured[[point["point_id"] for point in batch["points"]].index(negative_id)]
            )
            basis = "symmetric_half_difference"
            realized_step = 0.5 * (
                float(realized_coordinate(point_by_id[positive_id], experiment))
                - float(realized_coordinate(point_by_id[negative_id], experiment))
            )
            digital_realization_distinct = (
                point_by_id[positive_id]["final_sram_sha256"]
                != point_by_id[negative_id]["final_sram_sha256"]
            )
        elif positive_id is not None:
            # Each point bootstrap was already formed against the same resampled
            # reference curve in that replicate; subtracting a second reference
            # distribution here would double-count reference noise.
            distribution = bootstrap[positive_id][:, metric_column]
            estimate = measured[
                [point["point_id"] for point in batch["points"]].index(positive_id)
            ]
            basis = "positive_minus_reference"
            realized_step = float(realized_coordinate(point_by_id[positive_id], experiment))
            digital_realization_distinct = (
                point_by_id[positive_id]["final_sram_sha256"]
                != point_by_id[reference_id]["final_sram_sha256"]
            )
        else:
            distribution = -bootstrap[negative_id][:, metric_column]
            estimate = -measured[
                [point["point_id"] for point in batch["points"]].index(negative_id)
            ]
            basis = "reference_minus_negative"
            realized_step = -float(realized_coordinate(point_by_id[negative_id], experiment))
            digital_realization_distinct = (
                point_by_id[negative_id]["final_sram_sha256"]
                != point_by_id[reference_id]["final_sram_sha256"]
            )
        ci_low, ci_high = np.percentile(distribution, [2.5, 97.5])
        sign_ok = estimate * realized_step > 0
        resolved = bool(
            digital_realization_distinct
            and (ci_low > 0 or ci_high < 0)
            and sign_ok
        )
        resolution_rows.append(
            {
                "requested_magnitude": str(magnitude),
                "parameter_unit": (
                    "ns" if experiment in TIMING_EXPERIMENTS else point_rows[0]["parameter_unit"]
                ),
                "realized_symmetric_or_reference_step": realized_step,
                "measurement": (
                    estimate * 1e3 if experiment in TIMING_EXPERIMENTS else estimate
                ),
                "measurement_unit": (
                    "ps" if experiment in TIMING_EXPERIMENTS else "waveform_gain_fraction"
                ),
                "ci95_low": (
                    float(ci_low) * 1e3 if experiment in TIMING_EXPERIMENTS else float(ci_low)
                ),
                "ci95_high": (
                    float(ci_high) * 1e3 if experiment in TIMING_EXPERIMENTS else float(ci_high)
                ),
                "response_ratio": None if realized_step == 0 else float(estimate / realized_step),
                "resolved_from_zero_95pct": resolved,
                "digital_realization_distinct": digital_realization_distinct,
                "comparison_basis": basis,
            }
        )

    output = attempt / "precision_analysis"
    output.mkdir(exist_ok=True)
    formal.save_csv(output / "point_metrics.csv", point_rows)
    formal.save_csv(output / "resolution_table.csv", resolution_rows)
    formal.atomic_save_npy(output / "common_aux_time_s.npy", grid)
    formal.atomic_save_npy(output / "aux_aligned_frames_v.npy", traces)
    np.savez_compressed(
        output / "bootstrap_point_estimates.npz",
        **{point_id: values for point_id, values in bootstrap.items()},
    )

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    for point in batch["points"]:
        point_id = str(point["point_id"])
        axes[0, 0].plot(grid_ns[mask], point_means[point_id][mask] * 1e3,
                        lw=1.1, label=point_id)
        axes[0, 1].plot(grid_ns[mask],
                        (point_means[point_id][mask] - reference_mean[mask]) * 1e3,
                        lw=1.0, label=point_id)
    axes[0, 0].set_title("AUX-aligned point means; no waveform has been shifted")
    axes[0, 0].set_ylabel("CH3 (mV)")
    axes[0, 1].set_title("Point mean minus fixed zero-delta reference")
    axes[0, 1].set_ylabel("Difference (mV)")
    for axis in axes[0]:
        axis.set_xlabel("Time relative to AUX trigger (ns)")
        axis.grid(True)
    if len(batch["points"]) <= 24:
        axes[0, 0].legend(fontsize=7, ncol=2)

    if experiment in TIMING_EXPERIMENTS:
        measured_plot = measured * 1e3
        coordinate_plot = coordinate * 1e3
        fitted_plot = fitted * 1e3
        yerr = np.clip(
            np.asarray(
                [
                    [
                        row["bootstrap_median_time_shift_ps"] - row["time_shift_ci95_low_ps"],
                        row["time_shift_ci95_high_ps"] - row["bootstrap_median_time_shift_ps"],
                    ]
                    for row in point_rows
                ]
            ).T,
            0.0,
            None,
        )
        axes[1, 0].errorbar(
            coordinate_plot, measured_plot, yerr=yerr, fmt="o", capsize=2, ms=4
        )
        order = np.argsort(coordinate)
        if response_fit_available:
            axes[1, 0].plot(coordinate_plot[order], fitted_plot[order], "-")
        axes[1, 0].plot(
            coordinate_plot[order], coordinate_plot[order], "--", label="ideal 1:1"
        )
        axes[1, 0].set_xlabel("Realized timing command (ps)")
        axes[1, 0].set_ylabel("Measured fixed-template shift (ps)")
        residual_plot = linear_residual * 1e3
        residual_label = "Timing fit residual (ps)"
    else:
        measured_plot = measured
        coordinate_plot = coordinate
        fitted_plot = fitted
        yerr = np.clip(
            np.asarray(
                [
                    [
                        row["bootstrap_median_amplitude_gain_fraction"] - row["amplitude_gain_ci95_low"],
                        row["amplitude_gain_ci95_high"] - row["bootstrap_median_amplitude_gain_fraction"],
                    ]
                    for row in point_rows
                ]
            ).T,
            0.0,
            None,
        )
        axes[1, 0].errorbar(
            coordinate_plot, measured_plot, yerr=yerr, fmt="o", capsize=2, ms=4
        )
        order = np.argsort(coordinate)
        if response_fit_available:
            axes[1, 0].plot(coordinate_plot[order], fitted_plot[order], "-")
        axes[1, 0].set_xlabel(f"Realized amp delta ({point_rows[0]['parameter_unit']})")
        axes[1, 0].set_ylabel("Measured waveform gain fraction")
        residual_plot = linear_residual
        residual_label = "Gain fit residual"
    axes[1, 0].set_title(
        f"Response slope = {slope:.6g}"
        if response_fit_available
        else "No response slope: all realized coordinates are identical"
    )
    axes[1, 0].grid(True)
    if experiment in TIMING_EXPERIMENTS:
        axes[1, 0].legend(fontsize=8)
    axes[1, 1].axhline(0, color="black", lw=0.8)
    axes[1, 1].plot(coordinate, residual_plot, "o-")
    axes[1, 1].set_xlabel("Realized command delta")
    axes[1, 1].set_ylabel(residual_label)
    axes[1, 1].set_title("Residual after one global linear response model")
    axes[1, 1].grid(True)
    fig.savefig(output / "precision_summary.png", dpi=180)
    plt.close(fig)

    phase_ps = np.mod(xzero * 1e12, float(preamble["XINCR"]) * 1e12)
    fig, axis = plt.subplots(figsize=(8, 4.5), constrained_layout=True)
    axis.hist(phase_ps, bins=20, edgecolor="black")
    axis.set_xlabel("Per-frame XZERO phase modulo 40 ps (ps)")
    axis.set_ylabel("Frames")
    axis.set_title("Sub-sample trigger-phase coverage")
    axis.grid(True, axis="y")
    fig.savefig(output / "xzero_phase_histogram.png", dpi=180)
    plt.close(fig)

    summary = {
        "schema_version": 1,
        "created": formal.iso_now(),
        "run_id": contract["run_id"],
        "batch_key": batch_key,
        "batch_id": batch["batch_id"],
        "experiment": experiment,
        "reference_point_id": reference_id,
        "analysis_method": (
            "One fixed zero-delta template; joint linear regression on [template derivative, "
            "template amplitude, constant]. No waveform search, correlation, or fitted shift "
            "was applied to the saved data. The fitted time coefficient is a measurement."
        ),
        "active_roi_digital_ns": roi_digital,
        "active_roi_scope_ns": roi_scope_ns.tolist(),
        "path_delay_ns": path_delay_ns,
        "scope_sample_spacing_ps": float(preamble["XINCR"]) * 1e12,
        "reference_peak_to_peak_mv": reference_pp_v * 1e3,
        "derivative_smoothing_points": smoothing_points,
        "bootstrap_replicates": replicates,
        "bootstrap_block_shots": block_length,
        "global_response": {
            "fit_available": response_fit_available,
            "slope": None if slope is None else float(slope),
            "intercept": float(intercept),
            "residual_rms": float(np.sqrt(np.mean(linear_residual**2))),
        },
        "digital_realization_groups_by_sram_sha256": quantization_groups,
        "resolution_table": resolution_rows,
        "scope_qc_valid": True,
        "mapping_integrity_status": manifest["mapping_integrity_status"],
        "important_interpretation": (
            "resolved_from_zero_95pct is relative, batch-local detectability. It is not an "
            "absolute AWG accuracy or timebase calibration claim. Identical final SRAM hashes "
            "must be treated as the same digital realization regardless of requested value."
        ),
        "files": {
            "point_metrics": "point_metrics.csv",
            "resolution_table": "resolution_table.csv",
            "summary_plot": "precision_summary.png",
            "xzero_phase_histogram": "xzero_phase_histogram.png",
            "bootstrap": "bootstrap_point_estimates.npz",
        },
    }
    formal.atomic_write_json(output / "precision_analysis.json", summary)
    return summary


def _load_and_validate(path: Path) -> dict[str, Any]:
    contract = formal.load_json(path)
    validate_precision_contract(contract)
    return contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DPO70404C FastFrame capture and fixed-template analysis for timing/amp precision"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Validate the AWG handoff without touching the scope")

    preflight = sub.add_parser("preflight", help="Configure/query one batch without arming")
    preflight.add_argument("--batch", required=True)
    preflight.add_argument("--calibration-file", type=Path, required=True)
    preflight.add_argument("--vertical-scale-mv", type=float)
    preflight.add_argument("--vertical-offset-v", type=float)

    arm = sub.add_parser("arm", help="Arm and capture exactly one precision FastFrame batch")
    arm.add_argument("--batch", required=True)
    arm.add_argument("--calibration-file", type=Path, required=True)
    arm.add_argument("--capture-timeout-s", type=float, default=1800.0)
    arm.add_argument("--vertical-scale-mv", type=float)
    arm.add_argument("--vertical-offset-v", type=float)

    analyze = sub.add_parser("analyze", help="Analyze one completed precision attempt")
    analyze.add_argument("--attempt", type=Path, required=True)
    analyze.add_argument("--bootstrap-replicates", type=int)
    analyze.add_argument("--random-seed", type=int, default=20260816)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "analyze":
        summary = analyze_precision_attempt(
            args.attempt,
            bootstrap_replicates=args.bootstrap_replicates,
            random_seed=args.random_seed,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    config = args.config.resolve()
    contract = _load_and_validate(config)
    if args.command == "validate":
        formal.print_contract_summary(contract)
        print("\nPrecision contract validation passed. The scope was not opened.")
        return
    if args.batch not in contract["batches"]:
        parser.error(f"--batch must be one of: {', '.join(contract['batches'])}")
    calibration = args.calibration_file.resolve()
    if args.command == "preflight":
        path_delay, scale, offset, _ = formal.calibration_from_file(calibration)
        if args.vertical_scale_mv is not None:
            scale = args.vertical_scale_mv * 1e-3
        if args.vertical_offset_v is not None:
            offset = args.vertical_offset_v
        batch = contract["batches"][args.batch]
        with formal.ScopeIO(contract["scope_resource"]) as io:
            try:
                effective = formal.configure_scope(
                    io,
                    contract,
                    batch,
                    frame_count=int(batch["total_frames"]),
                    path_delay_ns=path_delay,
                    span_ns=float(batch["scope_span_ns"]),
                    vertical_scale_v=scale,
                    vertical_offset_v=offset,
                )
                events = formal.read_event_status(io, "precision_preflight")
                print(json.dumps(effective, ensure_ascii=False, indent=2))
                print(json.dumps(events, ensure_ascii=False, indent=2))
                print("Preflight passed. The scope was not armed.")
            finally:
                io.safe_stop()
        return
    attempt = arm_precision_batch(
        config,
        args.batch,
        calibration,
        output_root=args.output_root.resolve(),
        capture_timeout_s=args.capture_timeout_s,
        vertical_scale_v=(
            None if args.vertical_scale_mv is None else args.vertical_scale_mv * 1e-3
        ),
        vertical_offset_v=args.vertical_offset_v,
    )
    print(f"\nCapture saved: {attempt}")
    print("Run the analyze command only after the AWG execution log/frame map is reviewed.")


if __name__ == "__main__":
    main()
