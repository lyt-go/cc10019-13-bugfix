"""行为热力 ← 事件聚合 数据链路测试。

覆盖：口径选择、格值与色阶同源、缺失地区不补零、补算与查询并发、
新聚合失败时不得回退到模拟数据。
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services import behavior_heatmap
from app.services.behavior_heatmap import generate_heatmap, get_heatmap
from app.services.event_aggregate import aggregate_events, get_aggregate_result


class TestHeatmapChain(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()

    def test_cells_come_from_aggregate_rows(self) -> None:
        """热力格必须逐格对应聚合结果，且不再出现无来源的模拟数。"""
        aggregate_events("EA-004")
        result = generate_heatmap("BH-001")
        agg = get_aggregate_result("EA-004")

        self.assertEqual(result["source_aggregate"], "EA-004")
        self.assertEqual(result["data_source"], "event_aggregate")
        self.assertIsNotNone(agg)

        expected = {
            (str(row["region"]), row["time_grain"]): row["agg_value"]
            for row in agg["result_rows"]
        }
        actual = {
            (cell["dimension"], cell["time_slot"]): cell["value"]
            for cell in result["cells"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(result["max_intensity"], max(expected.values()))
        self.assertEqual(result["min_intensity"], min(expected.values()))

    def test_metric_selects_matching_caliber(self) -> None:
        """同一事件两种口径：人数模板消费 uv，次数模板消费 count。"""
        aggregate_events("EA-001")
        aggregate_events("EA-004")

        uv = generate_heatmap("BH-001")
        count = generate_heatmap("BH-002", metric="count")

        self.assertEqual(uv["metric"], "uv")
        self.assertEqual(uv["source_aggregate"], "EA-004")
        self.assertEqual(count["metric"], "count")
        self.assertEqual(count["source_aggregate"], "EA-001")
        for cell in uv["cells"]:
            self.assertEqual(cell["unit"], "独立人数")
        for cell in count["cells"]:
            self.assertEqual(cell["unit"], "页面次数")

    def test_count_values_never_enter_uv_scale(self) -> None:
        """页面次数不能参与人数色阶：人数格值不超过总用户数。"""
        aggregate_events("EA-001")
        aggregate_events("EA-004")
        result = generate_heatmap("BH-001")

        self.assertEqual(result["metric"], "uv")
        agg = get_aggregate_result("EA-004")
        expected_max = max(row["agg_value"] for row in agg["result_rows"])
        self.assertEqual(result["max_intensity"], expected_max)
        self.assertLessEqual(result["max_intensity"], 4200)

    def test_missing_regions_are_listed_not_zero(self) -> None:
        """未交付地区单列，不补 0，也不参与冷点。"""
        result = generate_heatmap("BH-001")

        self.assertEqual(
            result["uncovered_regions"], ["西南", "西北", "东北"]
        )
        covered = {cell["dimension"] for cell in result["cells"]}
        self.assertEqual(covered, {"华东", "华南", "华北", "华中"})
        self.assertFalse(covered & set(result["uncovered_regions"]))
        for cell in result["cells"]:
            self.assertNotIn(cell["dimension"], result["uncovered_regions"])
        cold = {item["dimension"] for item in result["cold_regions"]}
        self.assertFalse(cold & set(result["uncovered_regions"]))
        self.assertFalse(result["coverage"]["complete"])

    def test_recompute_refreshes_values_and_scale(self) -> None:
        """聚合补算后，热力查询自动重算，格值与色阶同步更新。"""
        first = generate_heatmap("BH-001")
        first_revision = first["source_revision"]

        aggregate_events("EA-004")
        refreshed = get_heatmap("BH-001")

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["source_revision"], first_revision + 1)
        self.assertEqual(refreshed["metric"], "uv")
        self.assertEqual(refreshed["source_aggregate"], "EA-004")

        agg = get_aggregate_result("EA-004")
        self.assertEqual(
            refreshed["max_intensity"],
            max(row["agg_value"] for row in agg["result_rows"]),
        )
        self.assertEqual(
            refreshed["color_legend"]["max"], refreshed["max_intensity"]
        )

    def test_concurrent_recompute_keeps_cells_and_scale_in_sync(self) -> None:
        """补算与查询并发：任意一次结果内部，色阶区间与格值一致。"""
        errors: list[str] = []
        stop = threading.Event()

        def recompute() -> None:
            try:
                while not stop.is_set():
                    aggregate_events("EA-004")
                    aggregate_events("EA-001")
            except Exception as exc:  # pragma: no cover - 失败时由断言给出
                errors.append(repr(exc))

        def query() -> None:
            try:
                for _ in range(60):
                    result = generate_heatmap("BH-001")
                    values = [cell["value"] for cell in result["cells"]]
                    if not values:
                        continue
                    self.assertEqual(max(values), result["max_intensity"])
                    self.assertEqual(min(values), result["min_intensity"])
                    self.assertEqual(
                        result["color_legend"]["max"], result["max_intensity"]
                    )
                    self.assertEqual(
                        result["color_legend"]["min"], result["min_intensity"]
                    )
                    self.assertEqual(result["metric"], "uv")
            except Exception as exc:
                errors.append(repr(exc))

        worker = threading.Thread(target=recompute)
        readers = [threading.Thread(target=query) for _ in range(3)]
        worker.start()
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()
        stop.set()
        worker.join()

        self.assertEqual(errors, [])

    def test_new_aggregate_failure_does_not_fall_back_to_mock(self) -> None:
        """聚合不可用时热力图报错，而不是呈现模拟数或把缺失地区当冷点。"""
        # 模拟补算失败：EV-001 的两份聚合都产不出来。
        original = behavior_heatmap.get_aggregate_result

        def failing(config_id: str):
            raise ValueError(f"aggregate failed: {config_id}")

        behavior_heatmap.get_aggregate_result = failing
        try:
            with self.assertRaises(ValueError):
                generate_heatmap("BH-001")
        finally:
            behavior_heatmap.get_aggregate_result = original

        self.assertIsNone(store.get_artifact("behavior_heatmap", "BH-001"))

    def test_heatmap_requires_source_aggregate(self) -> None:
        """没有任何聚合产物时，热力图直接失败（无来源不出图）。"""
        row = store.insert("behavior_heatmap", {
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "config_id": "BH-TMP",
            "event_id": "EV-404",
            "time_range": "2026-11-13/2026-11-15",
            "spatial_dim": "region",
            "agg_grain": "hour",
            "color_scale": "viridis",
            "metric": "uv",
            "output_image": "",
            "refresh_freq": "daily",
        })
        try:
            with self.assertRaises(ValueError):
                generate_heatmap("BH-TMP")
        finally:
            store.delete("behavior_heatmap", row["id"])

    def test_palette_changes_do_not_change_values(self) -> None:
        """色阶只影响颜色，不影响格值与口径。"""
        viridis = generate_heatmap("BH-001")
        config = store.find_by_field("behavior_heatmap", "config_id", "BH-001")
        original = config["color_scale"]
        config["color_scale"] = "reds"
        try:
            reds = generate_heatmap("BH-001")
        finally:
            config["color_scale"] = original

        self.assertEqual(
            [c["value"] for c in viridis["cells"]],
            [c["value"] for c in reds["cells"]],
        )
        self.assertNotEqual(
            [c["color"] for c in viridis["cells"]],
            [c["color"] for c in reds["cells"]],
        )
        self.assertEqual(reds["color_legend"]["scale"], "reds")


if __name__ == "__main__":
    unittest.main(verbosity=2)
