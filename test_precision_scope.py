import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

import formal_longitudinal_scope as formal
import precision_scope as precision


def make_points(experiment, values, shots, offset=0):
    points = []
    point_count = len(values)
    for point_index, value in enumerate(values):
        ranges = []
        for cycle in range(shots):
            frame = offset + cycle * point_count + point_index + 1
            ranges.append([frame, frame])
        text = str(value)
        point = {
            "point_id": f"{experiment}-{point_index}",
            "frame_ranges_1based": ranges,
            "target_start_ns": 100.0,
            "target_end_ns": 110.0,
            "marker_rising_edges_per_period": 1,
            "marker_sha256": "a" * 64,
            "final_sram_sha256": f"{point_index + 1:064x}",
        }
        if experiment == "timing_precision":
            point.update(
                {
                    "requested_delta_ns": text,
                    "realized_delta_ns": text,
                    "realized_timing_lag_offset_ns": text,
                }
            )
        else:
            point.update(
                {
                    "requested_amp_delta": text,
                    "realized_amp_delta": text,
                    "amp_unit": "native",
                }
            )
        points.append(point)
    return points


def make_batch(experiment, name, values, shots):
    points = make_points(experiment, values, shots)
    return {
        "batch_id": f"test-{name}",
        "experiment": experiment,
        "target_layers": [10],
        "total_frames": len(values) * shots,
        "shots_per_point": shots,
        "warmup_shots": 0,
        "discard_frames": [],
        "marker_fixed_across_points": True,
        "digital_center_ns": 105.0,
        "digital_coverage_ns": [95.0, 115.0],
        "scope_span_ns": 40.0,
        "confirmation": f"START test-{name}",
        "required_requested_magnitudes": sorted(
            {str(abs(value)) for value in values}, key=float
        ),
        "analysis": {
            "reference_point_id": points[values.index(0)]["point_id"],
            "active_roi_digital_ns": [100.0, 110.0],
            "derivative_smoothing_points": 7,
            "bootstrap_replicates": 100,
            "bootstrap_block_shots": 1,
        },
        "points": points,
    }


def make_contract():
    return {
        "schema_version": 2,
        "contract_status": "ready_for_scope_capture",
        "run_id": "precision-test",
        "awg_sample_rate_hz": 2.0e9,
        "awg_sram_samples": 32768,
        "awg_period_s": 16.384e-6,
        "marker_rise_sample": 200,
        "marker_rising_edges_per_period": 1,
        "scope_resource": "TEST",
        "scope_defaults": {
            "sample_rate_hz": 25.0e9,
            "trigger_level_v": 1.4,
        },
        "prior_path_delay": {"value_ns": 10.0},
        "batches": {
            "timing_coarse": make_batch(
                "timing_precision", "timing-coarse", [-0.05, -0.01, 0, 0.01, 0.05], 2
            ),
            "timing_fine": make_batch(
                "timing_precision", "timing-fine", [-0.005, -0.001, 0, 0.001, 0.005], 2
            ),
            "amp_coarse": make_batch(
                "amplitude_precision", "amp-coarse", [-0.01, -0.005, 0, 0.005, 0.01], 2
            ),
            "amp_fine": make_batch(
                "amplitude_precision",
                "amp-fine",
                [-0.001, -0.0005, -0.0001, 0, 0.0001, 0.0005, 0.001],
                2,
            ),
        },
    }


class PrecisionScopeTests(unittest.TestCase):
    def test_contract_and_interleaved_frame_ranges(self):
        contract = make_contract()
        precision.validate_precision_contract(contract)
        batch = contract["batches"]["timing_coarse"]
        mapping = formal.frame_map_for_batch(batch)
        self.assertEqual(len(mapping), 10)
        self.assertEqual([row["logical_frame_1based"] for row in mapping], list(range(1, 11)))
        self.assertEqual(
            [row["point_id"] for row in mapping[:5]],
            [point["point_id"] for point in batch["points"]],
        )
        self.assertEqual([row["shot_in_point_1based"] for row in mapping[:5]], [1] * 5)
        self.assertEqual([row["shot_in_point_1based"] for row in mapping[5:]], [2] * 5)

    def test_template_fit_recovers_joint_shift_and_gain(self):
        time_ns = np.linspace(-6, 6, 601)
        reference = (
            0.24
            + 0.035 * np.exp(-0.5 * ((time_ns + 1.1) / 0.8) ** 2)
            - 0.042 * np.exp(-0.5 * ((time_ns - 1.0) / 0.9) ** 2)
        )
        derivative = np.gradient(reference, time_ns)
        shift_ns = 0.017
        gain = 0.006
        offset = 0.0003
        trace = (
            reference
            - shift_ns * derivative
            + gain * (reference - np.mean(reference))
            + offset
        )
        result = precision.fit_template_coefficients(
            trace, reference, derivative, np.ones_like(time_ns, dtype=bool)
        )
        self.assertAlmostEqual(result["time_shift_ns"], shift_ns, places=10)
        self.assertAlmostEqual(result["amplitude_gain_fraction"], gain, places=10)
        self.assertAlmostEqual(result["offset_v"], offset, places=10)
        self.assertLess(result["residual_rms_v"], 1e-12)

    def test_template_contract_is_rejected(self):
        contract = make_contract()
        contract["template_only"] = True
        with self.assertRaisesRegex(ValueError, "template"):
            precision.validate_precision_contract(contract)

    def test_numeric_parameter_is_rejected_to_preserve_decimal_semantics(self):
        contract = make_contract()
        contract["batches"]["timing_fine"]["points"][0]["requested_delta_ns"] = -0.005
        with self.assertRaisesRegex(ValueError, "JSON string"):
            precision.validate_precision_contract(contract)

    def test_timing_response_contract_validation_skips_legacy_magnitudes(self):
        def point(pid, delta, ranges):
            return {
                "point_id": pid,
                "frame_ranges_1based": ranges,
                "target_start_ns": 7226.5,
                "target_end_ns": 7259.6,
                "marker_rising_edges_per_period": 1,
                "marker_sha256": "a" * 64,
                "final_sram_sha256": pid.encode().hex().ljust(64, "0")[:64],
                "requested_delta_ns": delta,
                "realized_delta_ns": delta,
                "realized_timing_lag_offset_ns": delta,
            }

        contract = {
            "schema_version": 2,
            "contract_status": "ready_for_scope_capture",
            "run_id": "timing-response-check",
            "awg_sample_rate_hz": 2e9,
            "awg_sram_samples": 32768,
            "awg_period_s": 16.384e-6,
            "marker_rise_sample": 200,
            "marker_rising_edges_per_period": 1,
            "scope_resource": "TEST",
            "scope_defaults": {"sample_rate_hz": 25e9, "trigger_level_v": 1.4},
            "prior_path_delay": {"value_ns": 10.0},
            "batches": {
                "timing_response_neg": {
                    "batch_id": "tr-neg",
                    "experiment": "timing_response",
                    "target_layers": [10],
                    "total_frames": 6,
                    "shots_per_point": 2,
                    "warmup_shots": 0,
                    "discard_frames": [],
                    "marker_fixed_across_points": True,
                    "digital_center_ns": 7243.25,
                    "digital_coverage_ns": [7223.5, 7263.0],
                    "scope_span_ns": 40.0,
                    "confirmation": "START tr-neg",
                    "required_requested_magnitudes": ["0", "0.002"],
                    "analysis": {
                        "reference_point_id": "p0",
                        "active_roi_digital_ns": [7226.5, 7259.6],
                        "derivative_smoothing_points": 7,
                        "bootstrap_replicates": 100,
                        "bootstrap_block_shots": 1,
                    },
                    "points": [
                        point("p0", "0", [[1, 1], [4, 4]]),
                        point("p_neg2", "-0.002", [[2, 2], [5, 5]]),
                        point("p_pos2", "0.002", [[3, 3], [6, 6]]),
                    ],
                }
            },
        }
        precision.validate_precision_contract(contract)

    def test_amplitude_capture_save_uses_no_timing_shift(self):
        contract = make_contract()
        batch = contract["batches"]["amp_coarse"]
        mapping = formal.frame_map_for_batch(batch)
        frames = len(mapping)
        points = 1000
        xincr = 40e-12
        center_s = 115e-9
        preamble = {
            "BYT_NR": 1,
            "BIT_NR": 8,
            "BN_FMT": "RI",
            "BYT_OR": "LSB",
            "PT_FMT": "Y",
            "XUNIT": "s",
            "YUNIT": "V",
            "WFID": "synthetic precision CH3",
            "NR_PT": points,
            "NR_FR": frames,
            "XINCR": xincr,
            "XZERO_SELECTED": 0.0,
            "PT_OFF": 499.5 - center_s / xincr,
            "YMULT": 0.0008,
            "YOFF": 0.0,
            "YZERO": 0.244,
        }
        raw = np.zeros((frames, points), dtype=np.int8)
        xzero = np.arange(frames) * 1e-12
        timestamps = [
            f"16 Aug 2026 10:00:{index:02d}.000 000 000 000"
            for index in range(frames)
        ]
        clean = {
            "stage": "test",
            "time": "test",
            "esr": "0",
            "events": "0,\"No events to report - queue empty\"",
        }
        effective = {
            "sample_rate": "25.0E9",
            "record_length": "1000",
            "interpolation_ratio": "1.0",
        }
        with TemporaryDirectory() as temporary:
            result = formal.save_capture(
                Path(temporary),
                contract,
                "amp_coarse",
                batch,
                mapping,
                raw,
                preamble,
                xzero,
                timestamps,
                effective,
                10.0,
                "synthetic",
                {"frames_acquired": frames},
                [clean, clean, clean],
                [],
            )
            self.assertTrue(result["qc"]["scope_valid"])
            summary = Path(temporary) / "point_summary.csv"
            self.assertIn("aux_trigger_only_no_timing_shift", summary.read_text())

    def test_end_to_end_precision_analysis_writes_resolution_outputs(self):
        contract = make_contract()
        batch = contract["batches"]["amp_coarse"]
        mapping = formal.frame_map_for_batch(batch)
        frames = len(mapping)
        points = 1000
        xincr = 40e-12
        center_s = 115e-9
        sample = np.arange(points)
        base = (
            24.0 * np.exp(-0.5 * ((sample - 430) / 38) ** 2)
            - 31.0 * np.exp(-0.5 * ((sample - 555) / 47) ** 2)
        )
        raw = np.empty((frames, points), dtype=np.int8)
        for frame_index, map_row in enumerate(mapping):
            delta = float(map_row["point"]["realized_amp_delta"])
            raw[frame_index] = np.rint(base * (1 + 8 * delta)).astype(np.int8)
        preamble = {
            "BYT_NR": 1,
            "BIT_NR": 8,
            "BN_FMT": "RI",
            "BYT_OR": "LSB",
            "PT_FMT": "Y",
            "XUNIT": "s",
            "YUNIT": "V",
            "WFID": "synthetic precision CH3",
            "NR_PT": points,
            "NR_FR": frames,
            "XINCR": xincr,
            "XZERO_SELECTED": 0.0,
            "PT_OFF": 499.5 - center_s / xincr,
            "YMULT": 0.0008,
            "YOFF": 0.0,
            "YZERO": 0.244,
        }
        xzero = np.arange(frames) * 1e-12
        timestamps = [
            f"16 Aug 2026 10:00:{index:02d}.000 000 000 000"
            for index in range(frames)
        ]
        clean = {
            "stage": "test",
            "time": "test",
            "esr": "0",
            "events": "0,\"No events to report - queue empty\"",
        }
        effective = {
            "sample_rate": "25.0E9",
            "record_length": "1000",
            "interpolation_ratio": "1.0",
        }
        with TemporaryDirectory() as temporary:
            attempt = Path(temporary)
            formal.atomic_write_json(attempt / "contract_snapshot.json", contract)
            formal.save_capture(
                attempt,
                contract,
                "amp_coarse",
                batch,
                mapping,
                raw,
                preamble,
                xzero,
                timestamps,
                effective,
                10.0,
                "synthetic",
                {"frames_acquired": frames},
                [clean, clean, clean],
                [],
            )
            formal.atomic_write_json(attempt / "CAPTURE_COMPLETE.json", {"scope_qc_valid": True})
            result = precision.analyze_precision_attempt(
                attempt, bootstrap_replicates=100, random_seed=7
            )
            self.assertEqual(result["experiment"], "amplitude_precision")
            self.assertTrue((attempt / "precision_analysis" / "point_metrics.csv").exists())
            self.assertTrue((attempt / "precision_analysis" / "resolution_table.csv").exists())
            self.assertTrue((attempt / "precision_analysis" / "precision_summary.png").exists())


if __name__ == "__main__":
    unittest.main()
