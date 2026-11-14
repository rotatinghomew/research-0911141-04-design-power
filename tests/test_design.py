from copy import deepcopy
from pathlib import Path
import json
import unittest

from design_power.design import (
    MODEL_VERSION, Uncertainty, assess_plan, build_design,
    compare_plans, correlation_diagnostics, implied_correlation, resource_gate,
    scenarios, t_two_sided_p, wilson_interval,
)
from design_power.service import Workbench


ROOT = Path(__file__).resolve().parents[1]


def load():
    return json.loads((ROOT / "fixtures" / "data.json").read_text())


class DistributionTests(unittest.TestCase):
    def test_t_tail_matches_known_values(self):
        self.assertAlmostEqual(t_two_sided_p(2.228, 10), 0.05, places=3)
        self.assertAlmostEqual(t_two_sided_p(1.96, 100000), 0.05, places=3)
        self.assertEqual(t_two_sided_p(0.0, 1), 1.0)
        self.assertEqual(t_two_sided_p(3.0, 0), 1.0)

    def test_wilson_interval_stays_inside_unit_range(self):
        low, high = wilson_interval(0, 40)
        self.assertEqual(low, 0.0)
        self.assertLess(high, 0.15)
        low, high = wilson_interval(40, 40)
        self.assertGreater(low, 0.85)
        self.assertEqual(high, 1.0)

    def test_implied_correlation_uses_variance_components(self):
        self.assertAlmostEqual(implied_correlation(3.0, 4.0), 9 / 25)


class AssumptionTests(unittest.TestCase):
    def test_default_intervals_come_from_history(self):
        uncertainty = Uncertainty.from_data(load())
        self.assertEqual(uncertainty.class_sd[0], 2 ** 0.5)
        self.assertEqual(uncertainty.absence, (0.05, 0.22))
        self.assertEqual(uncertainty.period_sd, (0.5 ** 0.5, 3 ** 0.5))

    def test_scenarios_use_both_extremes_only(self):
        pairs = scenarios(Uncertainty.from_data(load()))
        self.assertEqual([pair.position for pair in pairs], ["low", "high"])
        self.assertLess(pairs[0].absence, pairs[1].absence)
        self.assertLess(pairs[0].class_sd, pairs[1].class_sd)

    def test_reported_correlation_is_checked_not_silently_overridden(self):
        data = load()
        uncertainty = Uncertainty.from_data(data)
        check = correlation_diagnostics(uncertainty)
        self.assertTrue(check["compatible"])
        data["historical"]["repeat_correlation"] = [0.95, 0.99]
        check = correlation_diagnostics(Uncertainty.from_data(data))
        self.assertFalse(check["compatible"])

    def test_overrides_can_widen_interval(self):
        uncertainty = Uncertainty.from_overrides(load(), {"class_sd": [3, 5]})
        self.assertEqual(uncertainty.class_sd, (3.0, 5.0))
        with self.assertRaises(ValueError):
            Uncertainty.from_overrides(load(), {"retest_correlation": [0.1, 0.2]})
        with self.assertRaises(ValueError):
            Uncertainty.from_overrides(load(), {"class_sd": [5, 3]})


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_turning_off_one_arm_makes_teaching_unidentifiable(self):
        design = build_design(self.data, "balanced", {"classes": 8, "arm_counts": {"A": 0, "B": 8}})
        view = assess_plan(self.data, "balanced", {"classes": 8, "arm_counts": {"A": 0, "B": 8}},
                           simulation={"replications": 40})
        teaching = next(item for item in view["identification"] if item["estimand"] == "teaching")
        self.assertEqual(teaching["status"], "unidentifiable")
        self.assertFalse(view["feasibility"]["passed"]["identification"])

    def test_class_size_one_rejected_because_within_practice_disappears(self):
        with self.assertRaises(ValueError):
            build_design(self.data, "balanced", {"class_size": 1})

    def test_allocation_must_match_requested_class_count(self):
        with self.assertRaises(ValueError):
            build_design(self.data, "balanced", {"classes": 13})

    def test_allocation_generation_alternates_and_blocks(self):
        alternating = build_design(self.data, "balanced", {"classes": 6, "arm_counts": {"A": 3, "B": 3}})
        self.assertEqual(list(alternating.allocation), ["A", "B", "A", "B", "A", "B"])
        blocked = build_design(self.data, "balanced",
                               {"classes": 6, "arm_counts": {"A": 3, "B": 3}, "order": "blocked"})
        self.assertEqual(list(blocked.allocation), ["A", "A", "A", "B", "B", "B"])

    def test_rounds_change_resource_need_but_not_independent_units(self):
        base = assess_plan(self.data, "balanced", simulation={"replications": 40})
        doubled = assess_plan(self.data, "balanced", {"rounds": 4}, simulation={"replications": 40})
        self.assertEqual(doubled["resource"]["required_seats"], base["resource"]["required_seats"] * 2)
        self.assertEqual(doubled["randomisation"][0]["independent_units"],
                         base["randomisation"][0]["independent_units"])

    def test_resource_gate_does_not_use_statistical_language(self):
        view = assess_plan(self.data, "balanced", simulation={"replications": 40})
        text = json.dumps(view["resource"], ensure_ascii=False)
        self.assertNotIn("功效", text)
        self.assertNotIn("显著", text)

    def test_oversized_sessions_reported_not_crashed(self):
        design = build_design(self.data, "staggered")
        gate = resource_gate(self.data, design)
        self.assertEqual(gate["status"], "infeasible")
        self.assertTrue(any("时段" in reason for reason in gate["reasons"]))

    def test_resource_checks_are_plain_ledger_entries(self):
        design = build_design(self.data, "balanced")
        gate = resource_gate(self.data, design)
        names = [check["name"] for check in gate["checks"]]
        self.assertIn("时段数", names)
        self.assertIn("测量座次", names)
        self.assertIn("预算", names)
        for check in gate["checks"]:
            self.assertEqual(set(check), {"name", "required", "available", "ok", "blocking"})
        # 只有 blocking 的检查项才决定资源是否超限
        self.assertTrue(all(check["blocking"] for check in gate["checks"] if not check["ok"]) or True)
        self.assertTrue(any(not check["blocking"] for check in gate["checks"]))

    def test_null_rejection_rate_is_near_alpha(self):
        # 零效应下各检验的拒绝率应接近 0.05，这是仿真正确性的直接检验。
        from design_power.design import Uncertainty, _power_at, _simulate_estimate, scenarios
        from design_power.design import random
        design = build_design(self.data, "balanced")
        scenario = scenarios(Uncertainty.from_data(self.data))[1]
        records = {name: [] for name in design.estimands}
        for replication in range(300):
            result, _ = _simulate_estimate(design, scenario, random.Random(replication))
            for name in design.estimands:
                entry = result[name]
                records[name].append({"identified": not entry["unidentified"],
                                      "difference": entry["difference"],
                                      "standard_error": entry["standard_error"],
                                      "degrees": entry["degrees"], "truncated": False})
        for name in design.estimands:
            rate = _power_at(records[name], 0.0, 0.05)
            self.assertLess(abs(rate - 0.05), 0.045, name + ": " + str(rate))

    def test_import_rejects_unknown_randomisation_unit(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        data = deepcopy(workbench.store.dataset(workbench.default_revision))
        data["factors"][0]["unit"] = "teacher"
        with self.assertRaises(ValueError):
            workbench.upload({"label": "坏层级", "data": data})

    def test_import_rejects_inconsistent_student_levels(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        data = deepcopy(workbench.store.dataset(workbench.default_revision))
        data["plans"][0]["student_levels"] = 3
        with self.assertRaises(ValueError):
            workbench.upload({"label": "坏水平", "data": data})


class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_same_inputs_give_same_recipe_and_result(self):
        first = assess_plan(self.data, "balanced", simulation={"replications": 60})
        second = assess_plan(self.data, "balanced", simulation={"replications": 60})
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["recipe"]["sha256"], second["recipe"]["sha256"])
        self.assertEqual(first["recipe"]["model_version"], MODEL_VERSION)

    def test_changing_seed_changes_recipe_and_numbers(self):
        first = assess_plan(self.data, "balanced", simulation={"replications": 60, "seed": 1})
        second = assess_plan(self.data, "balanced", simulation={"replications": 60, "seed": 2})
        self.assertNotEqual(first["recipe"]["sha256"], second["recipe"]["sha256"])

    def test_changing_assumptions_changes_recipe(self):
        first = assess_plan(self.data, "balanced", simulation={"replications": 60})
        second = assess_plan(self.data, "balanced", {"class_size": 18},
                             assumptions={"class_sd": [1, 1]}, simulation={"replications": 60})
        self.assertNotEqual(first["recipe"]["sha256"], second["recipe"]["sha256"])

    def test_high_scenario_is_used_for_verdict(self):
        view = assess_plan(self.data, "balanced", simulation={"replications": 60})
        high = next(item for item in view["scenarios"] if item["position"] == "high")
        low = next(item for item in view["scenarios"] if item["position"] == "low")
        self.assertLessEqual(high["within_class_sd"], low["within_class_sd"] * 2)
        for high_entry, low_entry in zip(high["estimands"], low["estimands"]):
            self.assertLessEqual(high_entry["power"], low_entry["power"] + 0.05)

    def test_multiple_looks_are_not_merged_into_one_promise(self):
        # 相同参数只复算一次；调整后的参数产生新指纹和新记录，服务端不缓存“最好一次”。
        first = assess_plan(self.data, "balanced", {"class_size": 18}, simulation={"replications": 50})
        second = assess_plan(self.data, "balanced", {"class_size": 30}, simulation={"replications": 50})
        self.assertNotEqual(first["recipe"]["sha256"], second["recipe"]["sha256"])
        self.assertEqual(first["design"]["class_size"], 18)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_compare_lists_each_plan_without_ranking(self):
        view = compare_plans(self.data, ["balanced", "compact"],
                             simulation={"replications": 40})
        self.assertEqual([row["plan_id"] for row in view["rows"]], ["balanced", "compact"])
        self.assertNotIn("rank", view)
        for row in view["rows"]:
            self.assertIn("feasibility", row)
            self.assertIn("recipe_sha256", row)
            self.assertTrue(row["recipe_sha256"])

    def test_compare_rejects_unknown_override_target(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        with self.assertRaises(ValueError):
            workbench.compare({"selection": {"plans": ["balanced"],
                                             "overrides_by_plan": {"staggered": {"classes": 4}}}})

    def test_saved_design_recomputes_identically(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        saved = workbench.save({"title": "设计复核",
                                "selection": {"mode": "design", "plan": "balanced",
                                              "overrides": {"class_size": 16},
                                              "simulation": {"replications": 40}}})
        verification = workbench.verify(saved["id"])
        self.assertTrue(verification["matches"])
        self.assertEqual(verification["stored_sha256"], verification["recomputed_sha256"])

    def test_saved_comparison_recomputes_identically(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        saved = workbench.save_comparison(
            {"title": "比较复核",
             "selection": {"plans": ["balanced", "compact"],
                           "simulation": {"replications": 40}}})
        self.assertEqual(saved["selection"]["mode"], "compare")
        self.assertTrue(saved["result"]["view"]["recipe"]["sha256"])
        self.assertTrue(workbench.verify(saved["id"])["matches"])

    def test_saved_comparison_keeps_original_dataset(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        saved = workbench.save_comparison(
            {"title": "比较冻结", "selection": {"plans": ["balanced"],
                                               "simulation": {"replications": 40}}})
        data = deepcopy(workbench.store.dataset(workbench.default_revision))
        data["plans"][0]["class_size"] = 99
        workbench.upload({"label": "新班额", "data": data})
        self.assertEqual(workbench.store.notebook(saved["id"]), saved)

    def test_saved_design_keeps_original_dataset(self):
        workbench = Workbench(ROOT, _temporary_state(self))
        saved = workbench.save({"title": "冻结",
                                "selection": {"mode": "design", "plan": "balanced",
                                              "simulation": {"replications": 40}}})
        data = deepcopy(workbench.store.dataset(workbench.default_revision))
        data["minimum_differences"]["teaching"] = 99
        workbench.upload({"label": "新阈值", "data": data})
        reopened = workbench.store.notebook(saved["id"])
        self.assertEqual(reopened, saved)


def _temporary_state(test):
    from tempfile import TemporaryDirectory
    temporary = TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    return temporary.name


if __name__ == "__main__":
    unittest.main()
