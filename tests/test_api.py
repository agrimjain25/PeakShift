"""End-to-end tests: the real server, talking to a local stand-in for QuickML.

Run from the project root:  python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="peakshift-test-")
os.environ["USE_DATASTORE"] = "0"

import app as server_app  # noqa: E402
import forecast_core as core  # noqa: E402
import mock_quickml  # noqa: E402


def expected_mode(demand, hour):
    """Independent restatement of the problem-statement rules."""
    return "RENEWABLE_STORAGE" if (demand > 160 or 18 <= hour <= 22) else "MAIN_GRID"


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, cls.mock_state, url = mock_quickml.serve()
        cls.saved = {k: os.environ.get(k) for k in mock_quickml.env_for(url)}
        os.environ.update(mock_quickml.env_for(url))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_app.Handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_port
        with open(os.path.join(ROOT, "app", "data", "presets.json"), encoding="utf-8") as fh:
            cls.presets = json.load(fh)["presets"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.mock.shutdown()
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        core._batch_supported = None
        core._token.update({"value": None, "expires": 0.0})
        self.mock_state.fail = False

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def post_json(self, payload, path="/api/forecast"):
        status, headers, data = self.call("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        return status, json.loads(data)

    def preset(self, pid="heatwave"):
        return next(p for p in self.presets if p["id"] == pid)

    # ---- basics ---------------------------------------------------------------
    def test_page_and_assets_are_served(self):
        for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/styles.css", "text/css")):
            status, headers, data = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(kind, headers["Content-Type"])
            self.assertGreater(len(data), 500)

    def test_security_headers(self):
        _, headers, _ = self.call("GET", "/")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_health_and_config(self):
        status, _, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["model_credentials_configured"])
        status, _, data = self.call("GET", "/api/config")
        cfg = json.loads(data)
        self.assertEqual(len(cfg["presets"]), 4)
        self.assertEqual(cfg["rules"]["storage_threshold_kw"], 160.0)
        self.assertEqual(cfg["rules"]["overload_threshold_kw"], 200.0)
        self.assertEqual(cfg["rules"]["peak_window"], [18, 22])

    def test_unknown_paths_and_traversal_are_refused(self):
        for path in ("/nope", "/../app.py", "/%2e%2e/app.py", "/static/../../app.py", "/..%2fapp.py"):
            status, _, _ = self.call("GET", path)
            self.assertEqual(status, 404, path)
        status, _, _ = self.call("POST", "/api/other", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 404)

    # ---- forecasting ----------------------------------------------------------
    def test_forecast_follows_the_rules_for_every_hour(self):
        for p in self.presets:
            status, res = self.post_json({"rows": p["rows"], "source": "preset:" + p["id"]})
            self.assertEqual(status, 200)
            self.assertEqual(len(res["hours"]), 24)
            self.assertEqual(res["model"]["status"], "live")
            for h in res["hours"]:
                self.assertEqual(h["mode"], expected_mode(h["demand_kw"], h["hour"]), h["timestamp"])
                self.assertEqual(h["alert"], h["demand_kw"] >= 200, h["timestamp"])

    def test_signals_and_summary_are_consistent(self):
        status, res = self.post_json({"rows": self.preset()["rows"]})
        modes = [h["mode"] for h in res["hours"]]
        switches = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
        self.assertEqual(len(res["transitions"]), switches + 1)  # plus the initial mode
        self.assertEqual(res["summary"]["overload_hours"], sum(1 for h in res["hours"] if h["alert"]))
        self.assertEqual(sum(a["hours"] for a in res["alerts"]), res["summary"]["overload_hours"])
        self.assertEqual(res["signals"]["run_id"], res["run_id"])
        status, _, data = self.call("GET", "/api/signals/latest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["run_id"], res["run_id"])

    def test_csv_upload_is_cleaned(self):
        text = ("timestamp,ambient_temp_c,humidity_pct,solar_irradiance_wm2\n"
                "2015-06-18 01:00:00,,61,0\n"
                "2015-06-18 00:00:00,27,62,0\n"
                "2015-06-18 02:00:00,28,63,5\n"
                "2015-06-18 02:00:00,28,63,5\n")
        status, _, data = self.call("POST", "/api/forecast?capacity_kwh=2000", text.encode(), {"Content-Type": "text/csv"})
        res = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(len(res["hours"]), 3)
        self.assertEqual(res["cleaning"]["duplicates_removed"], 1)
        self.assertEqual(res["cleaning"]["filled"]["ambient_temp_c"], 1)
        self.assertTrue(res["cleaning"]["unsorted_fixed"])
        self.assertEqual(res["battery_config"]["capacity_kwh"], 2000.0)
        self.assertEqual(res["hours"][1]["ambient_temp_c"], 27.5)

    def test_stress_test_raises_demand(self):
        _, base = self.post_json({"rows": self.preset("spring")["rows"]})
        _, hot = self.post_json({"rows": self.preset("spring")["rows"], "temp_delta_c": 6})
        self.assertGreater(hot["summary"]["total_kwh"], base["summary"]["total_kwh"])
        self.assertEqual(hot["cleaning"]["what_if_temp_delta_c"], 6)

    def test_battery_settings_reach_the_simulation(self):
        _, small = self.post_json({"rows": self.preset()["rows"], "battery": {"capacity_kwh": 500}})
        _, big = self.post_json({"rows": self.preset()["rows"], "battery": {"capacity_kwh": 20000}})
        self.assertIsNotNone(small["summary"]["depleted_at"])
        self.assertIsNone(big["summary"]["depleted_at"])
        self.assertTrue(any(a["code"] == "BATTERY_DEPLETED" for a in small["advisories"]))

    def test_reference_runs_are_not_logged(self):
        _, _, data = self.call("GET", "/api/runs")
        before = len(json.loads(data)["runs"])
        self.post_json({"rows": self.preset()["rows"], "log": False})
        _, _, data = self.call("GET", "/api/runs")
        self.assertEqual(len(json.loads(data)["runs"]), before)
        self.post_json({"rows": self.preset()["rows"]})
        _, _, data = self.call("GET", "/api/runs")
        self.assertEqual(len(json.loads(data)["runs"]), before + 1)

    # ---- bad input ------------------------------------------------------------
    def test_bad_requests_get_plain_errors(self):
        status, body = self.post_json({"rows": "nope"})
        self.assertEqual((status, body["kind"]), (400, "bad_request"))
        status, body = self.post_json({"rows": []})
        self.assertEqual((status, body["kind"]), (400, "intake"))
        status, _, data = self.call("POST", "/api/forecast", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, data = self.call("POST", "/api/forecast", b"a,b\n1,2\n", {"Content-Type": "text/csv"})
        self.assertEqual(status, 400)
        self.assertIn("Missing column", json.loads(data)["error"])

    def test_oversized_upload_is_refused(self):
        status, _, _ = self.call("POST", "/api/forecast", b"x" * 1_200_000, {"Content-Type": "text/csv"})
        self.assertEqual(status, 413)

    def test_junk_battery_values_do_not_break_the_run(self):
        status, res = self.post_json({"rows": self.preset()["rows"], "battery": {"capacity_kwh": "lots", "initial_soc_pct": None, "pv_kwp": -4}})
        self.assertEqual(status, 200)
        self.assertEqual(res["battery_config"]["capacity_kwh"], 3500.0)

    # ---- model outage ---------------------------------------------------------
    def test_model_outage_is_a_503_with_a_clear_message(self):
        self.mock_state.fail = True
        rows = self.preset()["rows"]
        status, body = self.post_json({"rows": rows, "temp_delta_c": 3})  # not a saved preset
        self.assertEqual(status, 503)
        self.assertTrue(body["kind"].startswith("model_"))
        self.assertNotIn("Traceback", json.dumps(body))

    def test_saved_result_keeps_a_known_preset_working_during_an_outage(self):
        rows = self.preset()["rows"]
        clean, _ = core.clean_rows(rows)
        key = core.features_key(core.build_features(clean))
        original = core.load_cache
        core.load_cache = lambda: {"generated_at": "2026-10-27T10:00:00Z", "entries": {key: {"values": [150.0] * 24}}}
        self.mock_state.fail = True
        try:
            status, res = self.post_json({"rows": rows})
        finally:
            core.load_cache = original
        self.assertEqual(status, 200)
        self.assertEqual(res["model"]["status"], "cached")
        self.assertEqual(res["summary"]["peak_kw"], 150.0)


if __name__ == "__main__":
    unittest.main()
