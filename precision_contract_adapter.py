from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_AWG_CONTRACT = (
    PROJECT_ROOT
    / "data"
    / "awg_handoffs"
    / "PrecisionScan"
    / "precision-longitudinal-20260816_184351-de4cf4d3"
    / "precision_scope_contract.json"
)
DEFAULT_SCOPE_BASE = PROJECT_ROOT / "configs" / "formal_scope_batches.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "configs" / "precision_scope_contract.local.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def requested_decimal(point: dict[str, Any], experiment: str) -> Decimal:
    if experiment in ("timing_precision", "timing_response"):
        value = point.get("requested_delta_ns", point.get("requested_timing_lag_offset_ns"))
    else:
        value = point.get("requested_amp_delta", point.get("signed_requested_delta"))
    if value is None:
        raise ValueError(f"{point.get('point_id')}: missing requested coordinate")
    return Decimal(str(value))


def transform_point(point: dict[str, Any], experiment: str) -> dict[str, Any]:
    realized = point.get("realized")
    if not isinstance(realized, dict):
        raise ValueError(f"{point.get('point_id')}: missing realized object")
    ranges = point.get("frame_ranges_1based")
    if not isinstance(ranges, list):
        raise ValueError(f"{point.get('point_id')}: missing frame_ranges_1based")

    transformed: dict[str, Any] = {
        "point_id": str(point["point_id"]),
        "frame_ranges_1based": [
            [int(block["first_frame"]), int(block["last_frame"])] for block in ranges
        ],
        "target_start_ns": float(str(realized["target_start_ns"])),
        "target_end_ns": float(str(realized["target_end_ns"])),
        "marker_rising_edges_per_period": 1,
        "marker_sha256": str(point["marker_sha256"]),
        "final_sram_sha256": str(point["final_sram_sha256"]),
    }
    if experiment in ("timing_precision", "timing_response"):
        transformed.update(
            {
                "requested_delta_ns": str(realized["requested_delta_ns"]),
                "requested_timing_lag_absolute_ns": str(
                    realized["requested_timing_lag_absolute_ns"]
                ),
                "realized_delta_ns": str(realized["realized_delta_ns"]),
                "realized_timing_lag_absolute_ns": str(
                    realized["realized_timing_lag_absolute_ns"]
                ),
                "realized_timing_lag_offset_ns": str(
                    realized["realized_timing_lag_offset_ns"]
                ),
                "quantization_error_ns": str(realized.get("quantization_error_ns", "0")),
            }
        )
    else:
        transformed.update(
            {
                "requested_amp_delta": str(realized["requested_amp_delta"]),
                "realized_amp_delta": str(realized["realized_amp_delta"]),
                "base_amp": str(realized["base_amp"]),
                "amp_parameter": str(realized.get("amp_parameter", "q0ampback")),
                "amp_unit": str(
                    realized.get("amp_unit", "dimensionless native q9 longitudinal gate parameter")
                ),
                "requested_amp_absolute": str(realized.get("requested_amp_absolute", "")),
                "realized_amp_absolute": str(realized.get("realized_amp_absolute", "")),
                "quantization_error_amp": str(realized.get("quantization_error_amp", "0")),
            }
        )
    return transformed


def transform_batch(batch: dict[str, Any]) -> dict[str, Any]:
    experiment = str(batch["experiment"]).lower()
    if experiment not in {"timing_precision", "amplitude_precision", "timing_response"}:
        raise ValueError(f"{batch.get('batch_key')}: unsupported experiment {experiment!r}")
    coverage = batch["digital_coverage_ns"]
    analysis = batch["analysis"]
    roi_source = analysis.get("active_roi_digital_ns", batch.get("active_roi_digital_ns"))
    if isinstance(roi_source, dict):
        roi = [float(roi_source["start"]), float(roi_source["end"])]
    else:
        roi = [float(roi_source[0]), float(roi_source[1])]
    points = [transform_point(point, experiment) for point in batch["points"]]
    requested_magnitudes = sorted(
        {str(abs(requested_decimal(point, experiment))) for point in points},
        key=float,
    )
    return {
        "batch_id": str(batch["batch_id"]),
        "experiment": experiment,
        "target_layers": [int(layer) for layer in batch["target_layers"]],
        "total_frames": int(batch["total_frames"]),
        "shots_per_point": int(batch["shots_per_point"]),
        "warmup_shots": int(batch.get("warmup_shots", 0)),
        "discard_frames": list(batch.get("discard_frames", [])),
        "marker_fixed_across_points": bool(batch["marker_fixed_across_points"]),
        "digital_center_ns": float(batch["digital_center_ns"]),
        "digital_coverage_ns": [
            float(coverage["earliest"]),
            float(coverage["latest"]),
        ],
        "scope_span_ns": float(batch["scope_span_ns"]),
        "confirmation": str(batch["confirmation"]),
        "required_requested_magnitudes": requested_magnitudes,
        "analysis": {
            "reference_point_id": str(analysis["reference_point_id"]),
            "active_roi_digital_ns": roi,
            "derivative_smoothing_points": int(
                analysis.get("derivative_smoothing_points", 7)
            ),
            "bootstrap_replicates": int(analysis.get("bootstrap_replicates", 2000)),
            "bootstrap_block_shots": int(analysis.get("bootstrap_block_shots", 5)),
        },
        "points": points,
    }


def build_local_contract(
    awg_contract: dict[str, Any],
    scope_base: dict[str, Any],
    *,
    calibration: dict[str, Any] | None,
    awg_contract_path: Path,
    calibration_path: Path | None,
) -> dict[str, Any]:
    hardware = awg_contract.get("awg")
    if not isinstance(hardware, dict):
        raise ValueError("AWG contract is missing the top-level 'awg' object")
    if awg_contract.get("contract_status") != "ready_for_scope_capture":
        raise ValueError("AWG contract is not marked ready_for_scope_capture")
    if not isinstance(awg_contract.get("batches"), list) or not awg_contract["batches"]:
        raise ValueError("AWG contract has no batch list")
    scope_defaults = dict(scope_base["scope_defaults"])
    prior_path_delay = dict(scope_base["prior_path_delay"])
    if calibration is not None:
        prior_path_delay["value_ns"] = float(calibration["path_delay_ns"])
        prior_path_delay["status"] = "scope_calibration_override"
        prior_path_delay["source"] = str(calibration_path)
        scope_defaults["safe_vertical_scale_v_per_div"] = float(
            calibration["vertical_scale_v_per_div"]
        )
        scope_defaults["safe_vertical_offset_v"] = float(calibration["vertical_offset_v"])

    return {
        "schema_version": 2,
        "contract_status": "ready_for_scope_capture",
        "run_id": str(awg_contract["run_id"]),
        "awg_sample_rate_hz": float(hardware["sample_rate_ghz"]) * 1e9,
        "awg_sram_samples": int(hardware["sram_length_samples"]),
        "awg_period_s": float(hardware["period_ns"]) * 1e-9,
        "marker_rise_sample": int(hardware["marker_rise_sample"]),
        "marker_rising_edges_per_period": int(
            hardware["marker_rising_edges_per_period"]
        ),
        "scope_resource": scope_base["scope_resource"],
        "scope_defaults": scope_defaults,
        "prior_path_delay": prior_path_delay,
        "awg_source": {
            "run_id": str(awg_contract["run_id"]),
            "contract_path": str(awg_contract_path.resolve()),
            "marker_sha256": str(hardware.get("marker_sha256", "")),
        },
        "batches": {
            str(batch["batch_key"]): transform_batch(batch)
            for batch in awg_contract["batches"]
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert the AWG precision handoff into the scope-consumable contract schema"
    )
    parser.add_argument("--awg-contract", type=Path, default=DEFAULT_AWG_CONTRACT)
    parser.add_argument("--scope-base", type=Path, default=DEFAULT_SCOPE_BASE)
    parser.add_argument("--calibration-file", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    awg_contract = load_json(args.awg_contract.resolve())
    scope_base = load_json(args.scope_base.resolve())
    calibration = None
    if args.calibration_file is not None:
        calibration = load_json(args.calibration_file.resolve())
    local = build_local_contract(
        awg_contract,
        scope_base,
        calibration=calibration,
        awg_contract_path=args.awg_contract,
        calibration_path=args.calibration_file,
    )
    output = args.output.resolve()
    output.write_text(
        json.dumps(local, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {output}")
    print("Batch keys:", ", ".join(sorted(local["batches"])))
    for key, batch in sorted(local["batches"].items()):
        print(
            f"  {key:26s} frames={batch['total_frames']:3d} "
            f"shots/point={batch['shots_per_point']:2d} "
            f"roi={batch['analysis']['active_roi_digital_ns']}"
        )


if __name__ == "__main__":
    main()
