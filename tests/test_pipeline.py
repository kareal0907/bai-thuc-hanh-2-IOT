"""Kiem thu don vi (khong can MQTT/InfluxDB):  python -m unittest discover -s tests -v"""
import json
import sys
import tomllib
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gateway.common import parse_topic, validate_payload  # noqa: E402
from gateway.preprocess import preprocess_device  # noqa: E402

with (ROOT / "config.example.toml").open("rb") as fh:
    CFG = tomllib.load(fh)
TOPIC = "int14149/lab2/lab/esp32-01/telemetry"
NOW = 1_790_000_000_000


def payload(**over) -> bytes:
    data = {"device_id": "esp32-01", "ts": NOW - 150, "seq": 7, "temperature": 25.3, "humidity": 51.0,
            "light_lux": 480.5, "distance_cm": 99.5, "rssi": -60, "uptime_s": 35}
    data.update(over)
    return json.dumps(data).encode()


class ValidationTest(unittest.TestCase):
    def test_topic(self):
        self.assertEqual(parse_topic(TOPIC, "int14149/lab2"), ("lab", "esp32-01", "telemetry"))
        self.assertIsNone(parse_topic("other/lab/esp32-01/telemetry", "int14149/lab2"))
        self.assertIsNone(parse_topic("int14149/lab2/lab/telemetry", "int14149/lab2"))

    def test_valid(self):
        res = validate_payload(TOPIC, payload(), CFG, NOW)
        self.assertTrue(res.ok)
        self.assertEqual(res.issues, [])
        self.assertEqual(res.record.ts, NOW - 150)
        self.assertEqual(res.record.ts_source, "device")

    def test_rejects_structural_errors(self):
        self.assertEqual(validate_payload(TOPIC, b'{"device_id": "esp', CFG, NOW).issues, ["bad_json"])
        self.assertEqual(validate_payload(TOPIC, payload(device_id="x"), CFG, NOW).issues, ["device_id_mismatch"])
        self.assertEqual(validate_payload(TOPIC, payload(seq=0), CFG, NOW).issues, ["bad_seq"])
        self.assertEqual(validate_payload(TOPIC, payload(seq=True), CFG, NOW).issues, ["bad_seq"])
        empty = payload(temperature=None, humidity=None, light_lux=None, distance_cm=None)
        self.assertFalse(validate_payload(TOPIC, empty, CFG, NOW).ok)

    def test_field_level_errors_keep_sample(self):
        res = validate_payload(TOPIC, payload(humidity=180, distance_cm=None, light_lux="abc"), CFG, NOW)
        self.assertTrue(res.ok)
        self.assertIsNone(res.record.values["humidity"])
        self.assertIsNone(res.record.values["distance_cm"])
        self.assertIsNone(res.record.values["light_lux"])
        self.assertEqual(res.record.values["temperature"], 25.3)
        self.assertEqual(sorted(res.issues), ["bad_type:light_lux", "null:distance_cm", "out_of_range:humidity"])

    def test_clock(self):
        res = validate_payload(TOPIC, payload(ts=0), CFG, NOW)
        self.assertEqual((res.record.ts, res.record.ts_source), (NOW, "gateway"))
        res = validate_payload(TOPIC, payload(ts=NOW + 3_600_000), CFG, NOW)
        self.assertEqual(res.record.ts_source, "gateway")
        self.assertIn("clock_skew", res.issues)
        # mau gui bu tu bo dem 8 phut truoc van giu ts thiet bi
        res = validate_payload(TOPIC, payload(ts=NOW - 480_000), CFG, NOW)
        self.assertEqual(res.record.ts_source, "device")


class SequenceTrackingTest(unittest.TestCase):
    def make_collector(self):
        from collections import Counter

        from gateway.collector import Collector
        c = Collector.__new__(Collector)   # khong ket noi MQTT/InfluxDB
        c.last_seen, c.stats, c.ops_points = {}, Counter(), []
        return c

    def feed(self, c, seq, ts):
        rec = validate_payload(TOPIC, payload(seq=seq, ts=ts), CFG, NOW).record
        return c.track_sequence(rec)

    def test_gap_late_reset(self):
        c = self.make_collector()
        self.assertTrue(self.feed(c, 1, NOW - 50_000))
        self.assertTrue(self.feed(c, 2, NOW - 45_000))
        self.assertTrue(self.feed(c, 5, NOW - 30_000))       # mat seq 3, 4
        self.assertEqual(c.stats["q_gap"], 2)
        self.assertFalse(self.feed(c, 4, NOW - 35_000))      # seq 4 den tre
        self.assertEqual(c.stats["q_out_of_order"], 1)
        self.assertTrue(self.feed(c, 1, NOW - 10_000))       # khoi dong lai: seq lui, ts moi hon
        self.assertEqual(c.stats["q_reset"], 1)
        self.assertTrue(self.feed(c, 2, NOW - 5_000))
        self.assertEqual(c.stats["q_gap"], 2)


class PreprocessTest(unittest.TestCase):
    def make_raw(self, n=240):
        rng = np.random.default_rng(0)
        t = pd.date_range("2026-09-29T08:00:00Z", periods=n, freq="5s")
        df = pd.DataFrame({"_time": t, "device_id": "d", "site": "lab",
                           "temperature": 25 + np.sin(np.arange(n) / 40) + rng.normal(0, .1, n),
                           "humidity": 50 + rng.normal(0, .5, n),
                           "light_lux": 400 + rng.normal(0, 10, n),
                           "distance_cm": np.where((np.arange(n) > 100) & (np.arange(n) < 130), 40.0, 120.0),
                           "seq": np.arange(1, n + 1)})
        return df

    def test_spike_removed_and_gaps(self):
        raw = self.make_raw()
        raw.loc[50, "temperature"] = 60.0            # spike
        raw = raw.drop(index=range(70, 73))           # khoang trong ngan (3 mau) -> noi suy
        raw = raw.drop(index=range(150, 190))         # khoang trong dai (40 mau) -> giu NaN
        for method in ("zscore", "iqr"):
            out, outliers, report = preprocess_device(raw, CFG, method)
            self.assertIn(("temperature", 60.0), list(zip(outliers["field"], outliers["value"])), method)
            self.assertEqual(report["missing_before"]["temperature"], 43)
            self.assertGreaterEqual(report["imputed"]["temperature"], 4)   # 3 thieu + 1 spike
            self.assertGreater(report["missing_after"]["temperature"], 30)
            self.assertLess(out["temperature"].max(), 27)
            # buoc bac thang cua distance_cm khong bi coi la outlier
            self.assertNotIn("distance_cm", set(outliers["field"]))
            self.assertAlmostEqual(out["distance_cm"].min(), 40.0, delta=0.1)

    def test_resample_features(self):
        out, _, report = preprocess_device(self.make_raw(), CFG, "zscore")
        self.assertEqual(report["rows_processed"], 20)                     # 240 x 5 s = 20 phut
        self.assertTrue((out["completeness"] == 1.0).all())
        self.assertAlmostEqual(out["temperature_z"].mean(), 0, places=6)
        self.assertAlmostEqual(out["humidity_mm"].min(), 0)
        self.assertAlmostEqual(out["humidity_mm"].max(), 1)
        self.assertTrue(np.isnan(out["temperature_delta"].iloc[0]))

    def test_constant_signal_not_flagged(self):
        raw = self.make_raw()
        raw["light_lux"] = 500.0
        raw.loc[120:, "light_lux"] = 800.0            # slider Wokwi doi muc (bac thang)
        _, outliers, _ = preprocess_device(raw, CFG, "zscore")
        self.assertNotIn("light_lux", set(outliers["field"]))


if __name__ == "__main__":
    unittest.main()
