"""Tests for the transition controller and battery model.

Run from the project root:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import grid_logic as g  # noqa: E402


def ts(hour, day="2015-06-16"):
    return "{0} {1:02d}:00:00".format(day, hour)


def rows_for(demands, solar=0.0, start_hour=0):
    rows, kw = [], []
    for i, d in enumerate(demands):
        rows.append({"timestamp": ts((start_hour + i) % 24), "ambient_temp_c": 25.0,
                     "humidity_pct": 60.0, "solar_irradiance_wm2": solar})
        kw.append(d)
    return rows, kw


class DecisionRules(unittest.TestCase):
    def test_exactly_160_outside_window_is_main_grid(self):
        self.assertEqual(g.decide(160.0, 12)["mode"], g.MODE_GRID)

    def test_just_above_160_is_storage(self):
        self.assertEqual(g.decide(160.0001, 12)["mode"], g.MODE_STORAGE)

    def test_peak_window_is_inclusive_18_to_22(self):
        modes = {h: g.decide(100.0, h)["mode"] for h in (17, 18, 19, 20, 21, 22, 23)}
        self.assertEqual(modes[17], g.MODE_GRID)
        for h in (18, 19, 20, 21, 22):
            self.assertEqual(modes[h], g.MODE_STORAGE, "hour {0}".format(h))
        self.assertEqual(modes[23], g.MODE_GRID)

    def test_low_demand_outside_window_is_main_grid(self):
        for h in (0, 3, 6, 9, 15, 17, 23):
            self.assertEqual(g.decide(90.0, h)["mode"], g.MODE_GRID)

    def test_alert_threshold_is_200_inclusive(self):
        self.assertTrue(g.decide(200.0, 3)["alert"])
        self.assertFalse(g.decide(199.99, 3)["alert"])

    def test_alert_is_independent_of_mode(self):
        in_window = g.decide(250.0, 19)
        daytime = g.decide(250.0, 14)
        self.assertTrue(in_window["alert"] and daytime["alert"])
        self.assertEqual(in_window["mode"], g.MODE_STORAGE)
        self.assertEqual(daytime["mode"], g.MODE_STORAGE)

    def test_peak_window_without_alert(self):
        v = g.decide(120.0, 20)
        self.assertEqual(v["mode"], g.MODE_STORAGE)
        self.assertFalse(v["alert"])
        self.assertIn("PEAK_WINDOW_18_22", v["reasons"])

    def test_reasons_explain_the_decision(self):
        self.assertEqual(g.decide(170.0, 12)["reasons"], ["DEMAND_ABOVE_160_KW"])
        self.assertEqual(g.decide(170.0, 19)["reasons"], ["DEMAND_ABOVE_160_KW", "PEAK_WINDOW_18_22"])
        self.assertEqual(g.decide(100.0, 12)["reasons"], ["DEMAND_AT_OR_BELOW_160_KW_OUTSIDE_PEAK_WINDOW"])


class Transitions(unittest.TestCase):
    def test_typical_day_has_expected_switches(self):
        demand = [90] * 6 + [180] * 4 + [150] * 8 + [220] * 5 + [110]  # 24 hours
        rows, kw = rows_for(demand)
        plan = g.analyse(rows, kw)
        signals = [(t["signal"], t["hour"]) for t in plan["transitions"]]
        self.assertEqual(signals, [
            (g.SIGNAL_INITIAL, 0),
            (g.SIGNAL_TO_STORAGE, 6),
            (g.SIGNAL_TO_GRID, 10),
            (g.SIGNAL_TO_STORAGE, 18),
            (g.SIGNAL_TO_GRID, 23),
        ])

    def test_no_switch_when_mode_is_constant(self):
        rows, kw = rows_for([100] * 6)
        plan = g.analyse(rows, kw)
        self.assertEqual(len(plan["transitions"]), 1)

    def test_alert_hours_are_grouped(self):
        demand = [100] * 10 + [210, 230, 205] + [100] * 3 + [220] + [100] * 7
        rows, kw = rows_for(demand)
        plan = g.analyse(rows, kw)
        self.assertEqual(len(plan["alerts"]), 2)
        first, second = plan["alerts"]
        self.assertEqual((first["hours"], first["peak_kw"]), (3, 230))
        self.assertEqual((second["hours"], second["peak_kw"]), (1, 220))

    def test_decisions_use_demand_rounded_like_predictions_csv(self):
        rows, kw = rows_for([160.00004, 199.99996], start_hour=10)
        plan = g.analyse(rows, kw)
        self.assertEqual(plan["hours"][0]["mode"], g.MODE_GRID)       # 160.0000 after rounding
        self.assertFalse(plan["hours"][0]["alert"])
        self.assertTrue(plan["hours"][1]["alert"])                    # 200.0000 after rounding


class Probabilities(unittest.TestCase):
    def test_at_threshold_is_fifty_fifty(self):
        self.assertAlmostEqual(g.prob_above(160.0, 160.0, 10.0), 0.5, places=6)

    def test_far_from_threshold(self):
        self.assertGreater(g.prob_above(250.0, 200.0, 10.0), 0.999)
        self.assertLess(g.prob_above(100.0, 200.0, 10.0), 0.001)

    def test_close_calls_are_flagged(self):
        rows, kw = rows_for([158.0, 90.0, 205.0], start_hour=10)
        plan = g.analyse(rows, kw)
        self.assertTrue(plan["hours"][0]["mode_uncertain"])
        self.assertFalse(plan["hours"][1]["mode_uncertain"])
        self.assertTrue(plan["hours"][2]["alert_uncertain"])

    def test_confidence_fields(self):
        rows, kw = rows_for([158.0, 90.0, 230.0], start_hour=10)
        h = g.analyse(rows, kw)["hours"]
        self.assertTrue(0.5 < h[0]["mode_confidence"] < 0.7)  # 158 kW is called Main Grid, but only just
        self.assertGreater(h[1]["mode_confidence"], 0.99)
        self.assertGreater(h[2]["alert_confidence"], 0.99)
        rows, kw = rows_for([100.0], start_hour=20)
        self.assertEqual(g.analyse(rows, kw)["hours"][0]["mode_confidence"], 1.0)

    def test_peak_window_mode_is_never_uncertain(self):
        rows, kw = rows_for([158.0], start_hour=19)
        self.assertFalse(g.analyse(rows, kw)["hours"][0]["mode_uncertain"])


class Battery(unittest.TestCase):
    def setUp(self):
        self.cfg = g.normalise_battery({})

    def run_day(self, demand, solar=0.0, cfg=None):
        rows, kw = rows_for(demand, solar=solar)
        return g.analyse(rows, kw, cfg or {})

    def test_settings_are_clamped_and_junk_is_ignored(self):
        cfg = g.normalise_battery({"capacity_kwh": -5, "initial_soc_pct": 500, "bogus": 1, "pv_kwp": "abc"})
        self.assertEqual(cfg["capacity_kwh"], g.BATTERY_LIMITS["capacity_kwh"][0])
        self.assertEqual(cfg["initial_soc_pct"], 100.0)
        self.assertEqual(cfg["pv_kwp"], g.DEFAULT_BATTERY["pv_kwp"])
        self.assertNotIn("bogus", cfg)

    def test_soc_stays_within_physical_limits(self):
        plan = self.run_day([90] * 6 + [180] * 4 + [150] * 8 + [260] * 5 + [110], solar=150.0)
        for h in plan["hours"]:
            self.assertGreaterEqual(h["battery"]["soc_pct"], -0.01)
            self.assertLessEqual(h["battery"]["soc_pct"], 100.01)

    def test_battery_never_discharges_below_reserve(self):
        plan = self.run_day([300] * 24, cfg={"capacity_kwh": 1000, "initial_soc_pct": 50, "reserve_pct": 20})
        floor = 20.0 - 0.01
        for h in plan["hours"]:
            self.assertGreaterEqual(h["battery"]["soc_pct"], floor)

    def test_depletion_is_reported_with_grid_assist(self):
        plan = self.run_day([300] * 24, cfg={"capacity_kwh": 1000, "initial_soc_pct": 50})
        self.assertIsNotNone(plan["summary"]["depleted_at"])
        self.assertGreater(plan["summary"]["grid_assist_kwh"], 0)
        self.assertTrue(any(n["code"] == "BATTERY_DEPLETED" for n in plan["advisories"]))

    def test_load_is_always_fully_served(self):
        plan = self.run_day([90] * 6 + [180] * 4 + [150] * 8 + [260] * 5 + [110], solar=100.0)
        for h in plan["hours"]:
            b = h["battery"]
            served = b["pv_direct_kw"] + b["discharge_kw"] + (b["grid_import_kw"] - b["grid_charge_kw"])
            self.assertAlmostEqual(served, h["demand_kw"], places=1)

    def test_grid_charging_stays_under_160_kw_draw(self):
        plan = self.run_day([90] * 6, cfg={"initial_soc_pct": 10})
        for h in plan["hours"]:
            self.assertLessEqual(h["battery"]["grid_import_kw"], 160.01)

    def test_energy_balance_in_storage_hours(self):
        plan = self.run_day([200] * 5, cfg={"capacity_kwh": 5000, "initial_soc_pct": 100, "reserve_pct": 0})
        # 5 hours x 200 kW = 1000 kWh drawn from a 5000 kWh battery
        self.assertAlmostEqual(plan["hours"][-1]["battery"]["soc_kwh"], 4000.0, delta=1.0)

    def test_storage_lowers_grid_peak(self):
        plan = self.run_day([90] * 6 + [260] * 5, cfg={"capacity_kwh": 6000, "initial_soc_pct": 100})
        s = plan["summary"]
        self.assertLess(s["grid_peak_with_storage_kw"], s["grid_peak_without_storage_kw"])

    def test_right_size_finds_a_working_capacity(self):
        rows, kw = rows_for([250] * 10, solar=0.0)
        base = g.analyse(rows, kw, {"capacity_kwh": 500, "initial_soc_pct": 100, "max_discharge_kw": 1000})
        hours = base["hours"]
        cfg = g.normalise_battery({"capacity_kwh": 500, "initial_soc_pct": 100, "max_discharge_kw": 1000})
        plan = g.right_size_capacity(hours, cfg)
        self.assertTrue(plan["needed"] and plan["feasible"])
        cfg["capacity_kwh"] = plan["capacity_kwh"]
        self.assertLessEqual(sum(s["grid_assist_kw"] for s in g.simulate_battery(hours, cfg)), 0.5)

    def test_power_limit_is_reported_when_capacity_cannot_help(self):
        rows, kw = rows_for([300] * 6)
        base = g.analyse(rows, kw, {"max_discharge_kw": 100})
        plan = g.right_size_capacity(base["hours"], base["battery_config"])
        self.assertTrue(plan["needed"])
        self.assertFalse(plan["feasible"])


class Summary(unittest.TestCase):
    def test_summary_counts(self):
        demand = [90] * 6 + [180] * 4 + [150] * 8 + [220] * 5 + [110]
        rows, kw = rows_for(demand)
        s = g.analyse(rows, kw)["summary"]
        self.assertEqual(s["hours"], 24)
        self.assertEqual(s["peak_kw"], 220)
        self.assertEqual(s["overload_hours"], 5)
        self.assertEqual(s["storage_hours"], 9)
        self.assertEqual(s["grid_hours"], 15)

    def test_empty_input(self):
        plan = g.analyse([], [])
        self.assertEqual(plan["hours"], [])
        self.assertEqual(plan["summary"], {})


if __name__ == "__main__":
    unittest.main()
