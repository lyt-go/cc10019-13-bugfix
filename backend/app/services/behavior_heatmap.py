"""行为热力服务：消费事件聚合结果，生成热力格数据与色阶。

热力格只来自事件聚合产物，不再生成任何“模拟热力点”：

- 先按热力配置里的口径（uv=独立人数 / count=页面次数）选择聚合配置；
- 聚合值直接映射成格子的 intensity，色阶只基于已覆盖格子的取值区间计算；
- 聚合结果里尚未交付的维度值（例如只到了一部分地区）单独列在
  ``uncovered_regions`` 中，绝不补 0，也不会被当成冷点；
- 热力产物记录所消费聚合配置的 revision，聚合补算后重新查询会自动重算，
  保证格值与色阶同源、同步。
"""
from __future__ import annotations

from typing import Any
from collections import defaultdict

from app.store import store
from app.services.event_aggregate import get_aggregate_result

# 空间维度 -> 聚合结果里可能使用的维度字段
SPATIAL_DIM_FIELDS: dict[str, tuple[str, ...]] = {
    "page": ("page_url", "page"),
    "region": ("region", "ATTR-006"),
    "element": ("element", "element_id"),
}

METRIC_LABELS = {"uv": "独立人数", "count": "页面次数"}

# 只有已生效的聚合配置才交付数据；草稿/停用配置不参与热力计算。
ACTIVE_STATUS = "已生效"

# 色阶调色板（低 -> 高），按色阶名字取用。
PALETTES: dict[str, list[str]] = {
    "viridis": ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"],
    "plasma": ["#0d0887", "#5c01a6", "#9c179e", "#cc4778", "#ed7953", "#f0f921"],
    "blues": ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    "reds": ["#fff5f0", "#fcbba1", "#fb6a4a", "#cb181d", "#67000d"],
    "green": ["#f7fcf5", "#c7e9c0", "#74c476", "#238b45", "#00441b"],
}

LEGEND_STOPS = 5


def _palette(color_scale: str) -> list[str]:
    return PALETTES.get((color_scale or "").lower(), PALETTES["viridis"])


def _color_for(value: float, minimum: float, maximum: float, color_scale: str) -> str:
    """按色阶把数值映射成颜色；所有格子共用同一取值区间。"""
    palette = _palette(color_scale)
    if maximum <= minimum:
        ratio = 1.0 if maximum > 0 else 0.0
    else:
        ratio = (value - minimum) / (maximum - minimum)
    ratio = min(max(ratio, 0.0), 1.0)
    index = int(round(ratio * (len(palette) - 1)))
    return palette[index]


def _color_legend(minimum: float, maximum: float, color_scale: str) -> dict[str, Any]:
    palette = _palette(color_scale)
    stops: list[dict[str, Any]] = []
    for i in range(LEGEND_STOPS):
        ratio = i / (LEGEND_STOPS - 1)
        value = minimum + (maximum - minimum) * ratio
        index = int(round(ratio * (len(palette) - 1)))
        stops.append({"value": round(value, 2), "color": palette[index]})
    return {
        "scale": color_scale or "viridis",
        "min": minimum,
        "max": maximum,
        "stops": stops,
    }


def _spatial_field(agg: dict[str, Any], spatial_dim: str) -> str | None:
    dimensions = agg.get("agg_dimensions", [])
    for candidate in SPATIAL_DIM_FIELDS.get(spatial_dim, (spatial_dim,)):
        if candidate in dimensions:
            return candidate
    return None


def _resolve_metric(config: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """确定热力口径；未显式配置时按可用聚合推断，人数口径优先。"""
    requested = str(config.get("metric") or "").strip().lower()
    if requested:
        return requested
    functions = {str(a.get("agg_function", "")) for a in candidates}
    if "uv" in functions:
        return "uv"
    if "count" in functions:
        return "count"
    return "uv"


def _select_aggregate(
    config: dict[str, Any],
    event_id: str,
    spatial_dim: str,
    metric: str,
) -> tuple[dict[str, Any], str] | None:
    """挑选消费的聚合配置，返回 (产物, 空间维度字段)。"""
    aggregates = [
        a for a in store.list_artifacts("event_aggregate")
        if a.get("event_id") == event_id
    ]
    candidates: list[tuple[dict[str, Any], str]] = []
    for agg in aggregates:
        field = _spatial_field(agg, spatial_dim)
        if field is not None:
            candidates.append((agg, field))

    if not candidates:
        return None

    caliber_matches = [
        (agg, field) for agg, field in candidates
        if str(agg.get("agg_function", "")) == metric
    ]
    if not caliber_matches:
        # 页面次数不能拿来算人数色阶，反之亦然：口径不匹配时宁可失败也不混算。
        return None

    preferred_id = str(config.get("source_config_id") or "")
    if preferred_id:
        for agg, field in caliber_matches:
            if agg.get("config_id") == preferred_id:
                return agg, field

    def score(item: tuple[dict[str, Any], str]) -> tuple[int, str]:
        agg, field = item
        extras = len([d for d in agg.get("agg_dimensions", []) if d != field])
        return (extras, str(agg.get("config_id")))

    return sorted(caliber_matches, key=score)[0]


def _ensure_event_aggregates(event_id: str) -> None:
    """补齐该事件下已生效聚合配置的产物；失败的配置保持缺失，不伪造数据。"""
    for row in store.rows("event_aggregate"):
        if row.get("event_id") != event_id:
            continue
        if row.get("status") != ACTIVE_STATUS:
            continue
        config_id = row.get("config_id")
        if not config_id:
            continue
        if store.get_artifact("event_aggregate", config_id) is None:
            try:
                get_aggregate_result(config_id)
            except ValueError:
                continue


def _configured_universe(field: str, spatial_dim: str) -> list[str]:
    """维度全集：优先取属性字典的枚举值；地区维度兜底用字典表。"""
    attr = store.find_by_field("attr_dict", "attr_id", field)
    if attr is not None and attr.get("enum_values"):
        return [str(v) for v in attr["enum_values"]]
    if spatial_dim == "region":
        return ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]
    return []


def _coverage_for(
    agg: dict[str, Any],
    field: str,
    observed_dimensions: list[str],
    spatial_dim: str,
) -> dict[str, Any]:
    coverage = (agg.get("coverage") or {}).get(field)
    if coverage and coverage.get("universe"):
        universe = [str(v) for v in coverage["universe"]]
    else:
        universe = _configured_universe(field, spatial_dim)
    if not universe:
        universe = list(observed_dimensions)

    delivered = [v for v in universe if v in observed_dimensions]
    missing = [v for v in universe if v not in observed_dimensions]
    return {
        "dimension": field,
        "universe": universe,
        "delivered": delivered,
        "missing": missing,
        "complete": not missing,
    }


def _build_result(
    *,
    config_id: str,
    config: dict[str, Any],
    agg: dict[str, Any],
    field: str,
    metric: str,
    spatial_dim: str,
    agg_grain: str,
    time_range: str,
    color_scale: str,
) -> dict[str, Any]:
    rows = agg.get("result_rows", [])

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        dimension_value = row.get(field)
        if dimension_value in (None, "", "unknown"):
            continue
        slot = str(row.get("time_grain", "all"))
        grouped[(str(dimension_value), slot)].append(row)

    cells: list[dict[str, Any]] = []
    observed_dimensions: list[str] = []
    for (dimension_value, slot), group in sorted(grouped.items()):
        if dimension_value not in observed_dimensions:
            observed_dimensions.append(dimension_value)
        if len(group) > 1 and metric == "uv":
            # 聚合里还有其它维度时无法直接去重合并，避免把人数重复相加。
            raise ValueError(
                f"Aggregate {agg.get('config_id')} cannot be reduced to "
                f"{spatial_dim} + time for uv metric"
            )
        value = float(group[0].get("agg_value", 0)) if len(group) == 1 else sum(
            float(item.get("agg_value", 0)) for item in group
        )
        cells.append({
            "dimension": dimension_value,
            "time_slot": slot,
            "metric": metric,
            "value": value,
            "intensity": value,
            "unit": METRIC_LABELS.get(metric, metric),
        })

    values = [cell["value"] for cell in cells]
    maximum = max(values) if values else 0.0
    minimum = min(values) if values else 0.0

    for cell in cells:
        cell["color"] = _color_for(cell["value"], minimum, maximum, color_scale)

    coverage = _coverage_for(agg, field, observed_dimensions, spatial_dim)
    covered = coverage["delivered"] or list(observed_dimensions)
    missing = coverage["missing"]

    heatmap_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        heatmap_data[cell["dimension"]].append({
            "dimension": cell["dimension"],
            "time_slot": cell["time_slot"],
            "intensity": cell["intensity"],
            "value": cell["value"],
            "metric": metric,
            "unit": cell["unit"],
            "color": cell["color"],
        })

    # 维度汇总：次数可累加；人数取峰值时段，避免跨时段重复计人。
    totals: list[dict[str, Any]] = []
    for dimension_value in covered:
        dimension_cells = [c for c in cells if c["dimension"] == dimension_value]
        if not dimension_cells:
            continue
        if metric == "uv":
            total = max(c["value"] for c in dimension_cells)
        else:
            total = sum(c["value"] for c in dimension_cells)
        totals.append({"dimension": dimension_value, "value": total})
    totals.sort(key=lambda item: (item["value"], item["dimension"]))
    cold_dimensions = [
        {"dimension": item["dimension"], "value": item["value"]}
        for item in totals[:3]
    ]

    return {
        "config_id": config_id,
        "event_id": config["event_id"],
        "spatial_dim": spatial_dim,
        "agg_grain": agg_grain,
        "time_range": time_range,
        "metric": metric,
        "metric_label": METRIC_LABELS.get(metric, metric),
        "color_scale": color_scale,
        "source_config_id": str(config.get("source_config_id") or ""),
        "color_legend": _color_legend(minimum, maximum, color_scale),
        "heatmap_data": dict(heatmap_data),
        "cells": cells,
        "max_intensity": maximum,
        "min_intensity": minimum,
        "covered_regions": covered,
        "comparable_regions": list(covered),
        "uncovered_regions": missing,
        "missing_regions": missing,
        "coverage": {
            **coverage,
            "covered": covered,
            "missing": missing,
            "unit": METRIC_LABELS.get(metric, metric),
        },
        "cold_regions": cold_dimensions,
        "dimension_totals": totals,
        "source_aggregate": agg.get("config_id"),
        "source_revision": int(agg.get("revision", 0)),
        "source_aggregate_detail": {
            "config_id": agg.get("config_id"),
            "event_id": agg.get("event_id"),
            "agg_function": agg.get("agg_function"),
            "metric": metric,
            "agg_dimensions": agg.get("agg_dimensions", []),
            "time_grain": agg.get("time_grain"),
            "revision": int(agg.get("revision", 0)),
            "output_table": agg.get("output_table", ""),
            "filter_expr": agg.get("filter_expr", ""),
            "source_cleanse": agg.get("source_cleanse"),
            "delivered_values": agg.get("delivered_values", {}),
        },
        "data_source": "event_aggregate",
        "output_image": config.get("output_image", ""),
    }


def inspect_heatmap(config_id: str, metric: str | None = None) -> dict[str, Any]:
    """计算热力格但不落产物：供面板预检覆盖范围，避免 GET 触发状态流转。"""
    return _generate(config_id, metric=metric, persist=False)


def generate_heatmap(config_id: str, metric: str | None = None) -> dict[str, Any]:
    """生成行为热力图，格子与色阶均来自事件聚合结果。"""
    return _generate(config_id, metric=metric, persist=True)


def _generate(config_id: str, *, metric: str | None, persist: bool) -> dict[str, Any]:
    config = store.find_by_field("behavior_heatmap", "config_id", config_id)
    if config is None:
        raise ValueError(f"Heatmap config not found: {config_id}")

    event_id = config["event_id"]
    spatial_dim = config.get("spatial_dim", "page")
    agg_grain = config.get("agg_grain", "hour")
    time_range = config.get("time_range", "")
    color_scale = config.get("color_scale", "viridis")

    # 先补齐该事件下已生效的聚合产物，再挑选口径匹配的一份。
    _ensure_event_aggregates(event_id)

    candidates = [
        agg for agg in store.list_artifacts("event_aggregate")
        if agg.get("event_id") == event_id
    ]
    if not candidates:
        raise ValueError(
            f"No aggregate result for event {event_id}; heatmap has no source data"
        )

    requested_metric = (metric or str(config.get("metric") or "")).strip().lower()
    resolved = _resolve_metric({**config, "metric": requested_metric}, candidates)
    if resolved not in METRIC_LABELS:
        raise ValueError(f"Unsupported heatmap metric: {resolved}")

    # 聚合补算与热力查询并发时：读取快照与 revision 后计算，写入前再比对一次；
    # 若期间聚合被重算，则重新读取最新产物再算，格值与色阶始终同源。
    for _attempt in range(5):
        selection = _select_aggregate(config, event_id, spatial_dim, resolved)
        if selection is None:
            available = sorted({str(a.get("agg_function", "")) for a in candidates})
            raise ValueError(
                f"No {resolved} aggregate covers spatial dimension '{spatial_dim}' "
                f"for event {event_id}; available calibers: {available}"
            )
        agg, field = selection
        source_revision = int(agg.get("revision", 0))
        result = _build_result(
            config_id=config_id,
            config=config,
            agg=agg,
            field=field,
            metric=resolved,
            spatial_dim=spatial_dim,
            agg_grain=agg_grain,
            time_range=time_range,
            color_scale=color_scale,
        )
        if store.artifact_revision("event_aggregate", agg.get("config_id")) != source_revision:
            continue
        if not persist:
            result["persisted"] = False
            return result
        store.set_artifact("behavior_heatmap", config_id, result)
        store.update("behavior_heatmap", config["id"], {
            "status": "已完成",
            "pending": False,
        })
        return result

    raise ValueError(
        "Aggregate result changed while generating heatmap; retry the query"
    )


def _config_signature(config: dict[str, Any]) -> dict[str, str]:
    """热力配置里会影响格值/色阶的字段；任一变化都要求重新生成。"""
    return {
        "event_id": str(config.get("event_id", "")),
        "spatial_dim": str(config.get("spatial_dim", "")),
        "agg_grain": str(config.get("agg_grain", "")),
        "color_scale": str(config.get("color_scale", "")),
        "metric": str(config.get("metric") or "").strip().lower(),
        "source_config_id": str(config.get("source_config_id") or ""),
    }


def get_heatmap(config_id: str) -> dict[str, Any] | None:
    """获取热力图结果；来源聚合或热力配置变化过则自动重新生成。"""
    config = store.find_by_field("behavior_heatmap", "config_id", config_id)
    if config is None:
        return None

    artifact = store.get_artifact("behavior_heatmap", config_id)
    if artifact:
        signature = _config_signature(config)
        signature_stale = any(
            artifact.get(key) != value for key, value in signature.items()
        )
        configured_metric = signature["metric"]
        metric_stale = bool(configured_metric) and artifact.get("metric") != configured_metric
        source_id = artifact.get("source_aggregate")
        source_revision = int(artifact.get("source_revision", 0))
        revision_fresh = (
            bool(source_id)
            and store.artifact_revision("event_aggregate", source_id) == source_revision
        )
        if revision_fresh and not metric_stale and not signature_stale:
            return artifact

    try:
        return generate_heatmap(config_id)
    except ValueError:
        return None
