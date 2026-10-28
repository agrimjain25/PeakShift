"""Tests for data cleaning, features and the QuickML client (against a local stand-in).

Run from the project root:  python -m unittest discover -s tests -v
"""

import csv
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import forecast_core as core  # noqa: E402
import mock_quickml  # noqa: E402


def day(hours=24, start="2015-06-18", temp=30.0, hum=60.0, solar=0.0):
    return [{"timestamp": "{0} {1:02d}:00:00".format(start, h), "ambient_temp_c": temp,
             "humidity_pct": hum, "solar_irradiance_wm2": solar} for h in range(hours)]


class Cleaning(unittest.TestCase):
    def test_clean_data_passes_through_unchanged(self):
        rows, report = core.clean_rows(day())
        self.assertEqual(len(rows), 24)
        self.assertEqual(sum(report["filled"].values()), 0)
        self.assertEqual(report["warnings"], [])

    def test_missing_values_are_interpolated(self):
        raw = day()
        raw[1]["ambient_temp_c"] = None
        raw[2]["ambient_temp_c"] = ""
        raw[0]["ambient_temp_c"] = 30.0
        raw[3]["ambient_temp_c"] = 33.0
        rows, report = core.clean_rows(raw)
        self.assertAlmostEqual(rows[1]["ambient_temp_c"], 31.0)
        self.assertAlmostEqual(rows[2]["ambient_temp_c"], 32.0)
        self.assertEqual(report["filled"]["ambient_temp_c"], 2)

    def test_edges_are_filled_from_nearest_value(self):
        raw = day()
        raw[0]["humidity_pct"] = None
        raw[23]["humidity_pct"] = "nan"
        raw[1]["humidity_pct"] = 55
        raw[22]["humidity_pct"] = 70
        rows, _ = core.clean_rows(raw)
        self.assertEqual(rows[0]["humidity_pct"], 55)
        self.assertEqual(rows[23]["humidity_pct"], 70)

    def test_impossible_values_become_missing_then_filled(self):
        raw = day()
        raw[5]["ambient_temp_c"] = 999
        rows, report = core.clean_rows(raw)
        self.assertEqual(report["invalid_to_missing"]["ambient_temp_c"], 1)
        self.assertEqual(rows[5]["ambient_temp_c"], 30.0)

    def test_small_overshoots_are_clipped(self):
        raw = day()
        raw[3]["humidity_pct"] = 101.2
        raw[4]["solar_irradiance_wm2"] = -3
        rows, report = core.clean_rows(raw)
        self.assertEqual(rows[3]["humidity_pct"], 100.0)
        self.assertEqual(rows[4]["solar_irradiance_wm2"], 0.0)
        self.assertEqual(sum(report["clipped"].values()), 2)

    def test_duplicates_and_order(self):
        raw = day(6)
        raw.append(dict(raw[2]))
        raw.reverse()
        rows, report = core.clean_rows(raw)
        self.assertEqual([r["timestamp"][11:13] for r in rows], ["00", "01", "02", "03", "04", "05"])
        self.assertEqual(report["duplicates_removed"], 1)
        self.assertTrue(report["unsorted_fixed"])

    def test_unreadable_timestamps_are_skipped(self):
        raw = day(4)
        raw.append({"timestamp": "yesterday", "ambient_temp_c": 1, "humidity_pct": 1, "solar_irradiance_wm2": 1})
        rows, report = core.clean_rows(raw)
        self.assertEqual(len(rows), 4)
        self.assertEqual(report["dropped_bad_timestamp"], 1)

    def test_all_timestamps_bad_is_an_error(self):
        with self.assertRaises(core.IntakeError):
            core.clean_rows([{"timestamp": "x", "ambient_temp_c": 1, "humidity_pct": 1, "solar_irradiance_wm2": 1}])

    def test_empty_input_is_an_error(self):
        with self.assertRaises(core.IntakeError):
            core.clean_rows([])

    def test_whole_column_missing_uses_training_average(self):
        raw = day(3)
        for r in raw:
            r["solar_irradiance_wm2"] = None
        rows, report = core.clean_rows(raw)
        self.assertEqual(rows[0]["solar_irradiance_wm2"], core.FALLBACK_VALUE["solar_irradiance_wm2"])
        self.assertTrue(report["warnings"])

    def test_gaps_are_counted(self):
        raw = day(5)
        del raw[2]
        _, report = core.clean_rows(raw)
        self.assertEqual(report["gaps"], 1)

    def test_long_input_is_truncated_to_a_week(self):
        raw = []
        for d in range(1, 11):
            raw += day(24, start="2015-06-{0:02d}".format(d))
        rows, report = core.clean_rows(raw)
        self.assertEqual(len(rows), core.MAX_ROWS)
        self.assertEqual(report["truncated"], 240 - core.MAX_ROWS)

    def test_stress_test_moves_temperature_and_humidity_together(self):
        rows, _ = core.clean_rows(day(), temp_delta_c=5.0)
        self.assertAlmostEqual(rows[0]["ambient_temp_c"], 35.0)
        self.assertAlmostEqual(rows[0]["humidity_pct"], 60.0 + core.HUMIDITY_PER_DEGREE * 5.0, places=2)

    def test_stress_test_keeps_humidity_in_range(self):
        rows, _ = core.clean_rows(day(hum=99.0), temp_delta_c=-8.0)
        self.assertLessEqual(rows[0]["humidity_pct"], 100.0)

    def test_text_numbers_with_decimal_comma(self):
        raw = day(2)
        raw[0]["ambient_temp_c"] = "31,5"
        rows, _ = core.clean_rows(raw)
        self.assertEqual(rows[0]["ambient_temp_c"], 31.5)


class CsvIntake(unittest.TestCase):
    def test_standard_header(self):
        rows = core.parse_csv_text("timestamp,ambient_temp_c,humidity_pct,solar_irradiance_wm2\n2015-06-18 00:00:00,30,60,0\n")
        self.assertEqual(rows[0]["ambient_temp_c"], "30")

    def test_aliases_semicolons_and_bom(self):
        text = "﻿Time;Temperature;Humidity;Solar\n2015-06-18 00:00:00;30;60;0\n"
        rows = core.parse_csv_text(text)
        self.assertEqual(set(["timestamp", "ambient_temp_c", "humidity_pct", "solar_irradiance_wm2"]), set(rows[0]))

    def test_missing_column_is_explained(self):
        with self.assertRaises(core.IntakeError) as ctx:
            core.parse_csv_text("timestamp,ambient_temp_c\n2015-06-18 00:00:00,30\n")
        self.assertIn("humidity_pct", str(ctx.exception))

    def test_header_only_and_empty(self):
        with self.assertRaises(core.IntakeError):
            core.parse_csv_text("timestamp,ambient_temp_c,humidity_pct,solar_irradiance_wm2\n")
        with self.assertRaises(core.IntakeError):
            core.parse_csv_text("   \n")

    def test_starter_kit_file_parses_fully(self):
        with open(os.path.join(ROOT, "sample_test_input.csv"), encoding="utf-8") as fh:
            rows = core.parse_csv_text(fh.read())
        self.assertEqual(len(rows), 1000)

    def test_mixed_timestamp_formats(self):
        for text in ("2015-06-18 05:00:00", "2015-06-18T05:00:00", "2015-06-18 05:00", "18/06/2015 05:00"):
            self.assertEqual(core._parse_ts(text).hour, 5, text)


class Features(unittest.TestCase):
    def test_calendar_features(self):
        rows, _ = core.clean_rows(day())
        feats = core.build_features(rows)
        by_hour = {f["hour"]: f for f in feats}
        self.assertEqual([h for h in by_hour if by_hour[h]["is_peak"]], [18, 19, 20, 21, 22])
        self.assertEqual([h for h in by_hour if by_hour[h]["is_morning_rush"]], [6, 7, 8, 9])
        self.assertEqual(feats[0]["day_of_week"], 3)  # Thursday 18 June 2015, Monday = 0

    def test_feature_order_matches_training_file(self):
        with open(os.path.join(ROOT, "model", "train_enriched.csv"), encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header[:-1], core.MODEL_FEATURES)

    def test_features_match_the_training_file_exactly(self):
        """Rebuild the features for the first training rows and compare with train_enriched.csv."""
        train = os.path.join(ROOT, "model", "train_enriched.csv")
        source = os.path.join(ROOT, "train_telemetry.csv")
        if not os.path.exists(source):
            self.skipTest("train_telemetry.csv is not in the project folder")
        with open(source, encoding="utf-8") as a, open(train, encoding="utf-8") as b:
            raw = list(csv.DictReader(a))[:500]
            enriched = list(csv.DictReader(b))[:500]
        for r, e in zip(raw, enriched):
            f = core.build_features([{"timestamp": r["timestamp"], "ambient_temp_c": float(r["ambient_temp_c"]),
                                      "humidity_pct": float(r["humidity_pct"]),
                                      "solar_irradiance_wm2": float(r["solar_irradiance_wm2"])}])[0]
            for name in core.MODEL_FEATURES:
                self.assertAlmostEqual(f[name], float(e[name]), places=6, msg=name)


class PredictScriptParity(unittest.TestCase):
    """predict.py (used by the judges) and the app must build identical model inputs."""

    def test_same_features_as_predict_py(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas is not installed")
        sys.path.insert(0, ROOT)
        import predict
        df = pd.read_csv(os.path.join(ROOT, "sample_test_input.csv"))
        df = df.head(core.MAX_ROWS)  # the app caps a request at one week; predict.py has no cap
        expected = predict.clean_and_build_features(df)
        rows, _ = core.clean_rows(df.to_dict(orient="records"))
        ours = core.build_features(rows)
        self.assertEqual(len(expected), len(ours))
        for name in core.MODEL_FEATURES:
            for a, b in zip(expected[name].tolist(), [f[name] for f in ours]):
                self.assertAlmostEqual(float(a), float(b), places=4, msg=name)


class QuickMLClient(unittest.TestCase):
    def setUp(self):
        core._batch_supported = None
        core._token.update({"value": None, "expires": 0.0})
        self._saved = {k: os.environ.get(k) for k in mock_quickml.env_for("http://x")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.server.shutdown()
        core._batch_supported = None

    def start(self, **kwargs):
        self.server, self.state, url = mock_quickml.serve(**kwargs)
        os.environ.update(mock_quickml.env_for(url))

    def features(self):
        rows, _ = core.clean_rows(day())
        return core.build_features(rows)

    def test_batch_mode_is_one_call_and_one_token(self):
        self.start()
        values, meta = core.call_quickml(self.features())
        self.assertEqual(len(values), 24)
        self.assertEqual(meta["mode"], "batch")
        self.assertEqual(self.state.calls, 1)
        self.assertEqual(self.state.token_requests, 1)

    def test_falls_back_to_one_call_per_row(self):
        self.start(batch=False)
        values, meta = core.call_quickml(self.features())
        self.assertEqual(len(values), 24)
        self.assertEqual(meta["mode"], "per-row")
        self.assertEqual(self.state.token_requests, 1)  # parallel threads share one sign-in
        # the next call remembers that batches are not accepted
        calls_before = self.state.calls
        core.call_quickml(self.features())
        self.assertEqual(self.state.calls - calls_before, 24)

    def test_values_follow_input_order(self):
        self.start()
        feats = self.features()
        values, _ = core.call_quickml(feats)
        for f, v in zip(feats, values):
            self.assertAlmostEqual(v, mock_quickml.demand(f), places=6)

    def test_credentials_are_sent_as_headers(self):
        self.start()
        core.call_quickml(self.features())
        headers = self.state.last_headers
        self.assertEqual(headers["authorization"], "Zoho-oauthtoken mock-token")
        self.assertEqual(headers["x-quickml-endpoint-key"], "test-key")
        self.assertEqual(headers["catalyst-org"], "test-org")
        self.assertEqual(headers["environment"], "Development")

    def test_outage_raises_a_clear_error(self):
        self.start(fail=True)
        with self.assertRaises(core.QuickMLError):
            core.predict_demand(self.features())

    def test_saved_result_is_used_only_when_the_endpoint_is_down(self):
        self.start(fail=True)
        feats = self.features()
        key = core.features_key(feats)
        original = core.load_cache
        core.load_cache = lambda: {"generated_at": "2026-10-27", "entries": {key: {"values": [111.0] * 24}}}
        try:
            values, meta = core.predict_demand(feats)
        finally:
            core.load_cache = original
        self.assertEqual(meta["status"], "cached")
        self.assertEqual(values, [111.0] * 24)

    def test_missing_credentials_are_reported(self):
        self.start()
        for k in ("ZOHO_REFRESH_TOKEN", "QUICKML_ACCESS_TOKEN"):
            os.environ.pop(k, None)
        with self.assertRaises(core.QuickMLError) as ctx:
            core.call_quickml(self.features())
        self.assertEqual(ctx.exception.kind, "config")


if __name__ == "__main__":
    unittest.main()
