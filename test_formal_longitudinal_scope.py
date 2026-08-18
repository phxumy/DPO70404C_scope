import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

import formal_longitudinal_scope as formal


class FormalScopePureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = formal.load_json(
            Path(__file__).resolve().parent / "configs" / "formal_scope_batches.json"
        )

    def test_contract_and_formal_maps(self):
        formal.validate_contract(self.contract)
        self.assertEqual(set(self.contract["batches"]), {
            "gatelen",
            "timinglag_l1",
            "timinglag_l10",
            "timinglag_l20",
        })
        for batch in self.contract["batches"].values():
            mapping = formal.frame_map_for_batch(batch)
            self.assertEqual(len(mapping), 120)
            self.assertEqual(mapping[0]["logical_frame_1based"], 1)
            self.assertEqual(mapping[-1]["logical_frame_1based"], 120)
            self.assertEqual(mapping[0]["shot_in_point_1based"], 1)
            self.assertEqual(mapping[19]["shot_in_point_1based"], 20)
            self.assertEqual(mapping[20]["shot_in_point_1based"], 1)

    def test_preview_map_has_six_frames(self):
        mapping = formal.frame_map_for_batch(
            self.contract["batches"]["gatelen"], shots_per_point=1
        )
        self.assertEqual(len(mapping), 6)
        self.assertEqual(
            [row["point_id"] for row in mapping],
            [point["point_id"] for point in self.contract["batches"]["gatelen"]["points"]],
        )

    def test_timestamp_parser_preserves_picoseconds(self):
        first = formal.timestamp_decimal("15 Aug 2026 19:29:35.581 280 761 677")
        second = formal.timestamp_decimal("15 Aug 2026 19:29:35.581 297 145 677")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(str(second - first), "0.000016384000")

    def test_time_and_voltage_formula(self):
        raw = np.asarray([[-2, -1, 0, 1]], dtype=np.int8)
        preamble = {
            "XINCR": 40e-12,
            "PT_OFF": -10.0,
            "YZERO": 0.0,
            "YMULT": 1e-3,
            "YOFF": -1.0,
        }
        xzero = np.asarray([5e-12])
        times, volts = formal.build_time_and_voltage(raw, preamble, xzero)
        expected_time = 5e-12 + 40e-12 * (np.arange(4) + 10)
        np.testing.assert_allclose(times[0], expected_time)
        np.testing.assert_allclose(volts[0], [-1e-3, 0.0, 1e-3, 2e-3])

    def test_synthetic_six_frame_save_pipeline(self):
        batch = self.contract["batches"]["gatelen"]
        mapping = formal.frame_map_for_batch(batch, shots_per_point=1)
        path_delay_ns = 10.853
        frame_count = len(mapping)
        point_count = 1000
        center_s = (batch["digital_center_ns"] + path_delay_ns) * 1e-9
        xincr = 40e-12
        pt_off = 499.5 - center_s / xincr
        preamble = {
            "BYT_NR": 1,
            "BIT_NR": 8,
            "BN_FMT": "RI",
            "BYT_OR": "LSB",
            "PT_FMT": "Y",
            "XUNIT": "s",
            "YUNIT": "V",
            "WFID": "synthetic CH3",
            "NR_PT": point_count,
            "NR_FR": frame_count,
            "XINCR": xincr,
            "XZERO_SELECTED": 0.0,
            "PT_OFF": pt_off,
            "YMULT": 0.0008,
            "YOFF": 0.0,
            "YZERO": 0.2432,
        }
        raw = np.zeros((frame_count, point_count), dtype=np.int8)
        xzero = np.arange(frame_count, dtype=float) * 1e-12
        timestamps = [
            f"16 Aug 2026 10:00:00.{20 * index:03d} 000 000 000"
            for index in range(frame_count)
        ]
        effective = {
            "sample_rate": "25.0000E+9",
            "record_length": "1000",
            "interpolation_ratio": "1.0000",
        }
        clean_event = {
            "stage": "synthetic",
            "time": "test",
            "esr": "0",
            "events": "0,\"No events to report - queue empty\"",
        }
        with TemporaryDirectory() as temporary:
            attempt = Path(temporary)
            result = formal.save_capture(
                attempt,
                self.contract,
                "gatelen",
                batch,
                mapping,
                raw,
                preamble,
                xzero,
                timestamps,
                effective,
                path_delay_ns,
                "synthetic_test",
                {"frames_acquired": frame_count},
                [clean_event, clean_event, clean_event],
                [],
            )
            self.assertTrue(result["qc"]["scope_valid"])
            self.assertTrue((attempt / "raw_frames.npy").exists())
            self.assertTrue((attempt / "frame_index.csv").exists())
            self.assertTrue((attempt / "plots" / "point_mean_overlays.png").exists())


if __name__ == "__main__":
    unittest.main()
