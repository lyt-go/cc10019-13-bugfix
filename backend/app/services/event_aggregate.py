"""事件聚合引擎：消费清洗后的事件数据，执行聚合计算。

一个事件可以有多份聚合配置（例如同一页面浏览事件的“页面次数”和“地区独立人数”
两种口径）。聚合产物除了聚合值，还要带上口径、维度和覆盖范围，
下游（行为热力）才能判断自己消费的是哪一份结果、哪些维度值尚未交付。
"""
from __future__ import annotations

import random
from typing import Any
from collections import defaultdict

from app.store import store
from app.schemas import AggResult

# 地区维度字典：与属性字典 ATTR-006 的枚举值保持一致。
REGION_UNIVERSE = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]

# 随机生成是固定种子的：同一事件每次聚合得到的数值一致，便于核对。
_USER_POOL = 4200
_EVENTS_PER_USER = 3
_BASE_TIME = 1794528000  # 2026-11-13 00:00 UTC
_PAGES = [f"/page{i}" for i in range(5)]
_REGION_WEIGHTS = [24, 20, 14, 11, 12, 9, 10]


def _generate_cleaned_events(event_id: str, user_count: int = _USER_POOL) -> list[dict[str, Any]]:
    """生成清洗后的事件数据（固定种子，结果可复现）。

    每个用户在同一小时重复上报多条事件：人数口径（uv）按用户去重，
    次数口径（count）按事件计数，两者因此可以区分。
    """
    rng = random.Random(f"cleaned-events::{event_id}")
    events: list[dict[str, Any]] = []
    for user_idx in range(1, user_count + 1):
        user_id = f"U{user_idx:05d}"
        region = rng.choices(REGION_UNIVERSE, weights=_REGION_WEIGHTS, k=1)[0]
        page = rng.choice(_PAGES)
        hour = rng.randrange(24)
        day = rng.randrange(3)
        channel = ["app", "web", "miniapp", "h5"][user_idx % 4]
        device = ["ios", "android", "pc", "other"][user_idx % 4]
        level = ["普通", "银卡", "金卡", "钻石"][user_idx % 4]
        value = float(user_idx * 10.5)
        for rep in range(_EVENTS_PER_USER):
            events.append({
                "user_id": user_id,
                "event_id": event_id,
                "timestamp": _BASE_TIME + day * 86400 + hour * 3600 + rep * 60,
                "page_url": page,
                "item_id": f"ITEM-{user_idx % 20:03d}",
                "region": region,
                "ATTR-001": channel,
                "ATTR-003": value,
                "ATTR-004": device,
                "ATTR-005": level,
                "ATTR-006": region,
                "value": value,
            })
    return events


def _dimension_universe(dimension: str) -> list[str] | None:
    """维度的合法值全集：优先取属性字典的枚举值。"""
    attr = store.find_by_field("attr_dict", "attr_id", dimension)
    if attr is not None and attr.get("enum_values"):
        return [str(v) for v in attr["enum_values"]]
    if dimension in ("region", "ATTR-006"):
        return list(REGION_UNIVERSE)
    return None


def _apply_filter(events: list[dict[str, Any]], filter_expr: str) -> list[dict[str, Any]]:
    """应用过滤表达式（当前支持 ATTR-003 > 0 这类简单条件）。"""
    if not filter_expr:
        return events
    if "ATTR-003" in filter_expr:
        return [e for e in events if float(e.get("ATTR-003", e.get("value", 0)) or 0) > 0]
    return events


def _apply_delivery(events: list[dict[str, Any]], delivered: dict[str, list[str]]) -> list[dict[str, Any]]:
    """按已交付的维度值裁剪输入，模拟上游只到了一部分数据。"""
    if not delivered:
        return events
    filtered = []
    for event in events:
        keep = True
        for dimension, values in delivered.items():
            if values and str(event.get(dimension, "")) not in {str(v) for v in values}:
                keep = False
                break
        if keep:
            filtered.append(event)
    return filtered


def _aggregate_value(agg_function: str, evts: list[dict[str, Any]]) -> float:
    if agg_function == "count":
        return len(evts)
    if agg_function == "sum":
        return sum(float(e.get("value", 0)) for e in evts)
    if agg_function == "avg":
        vals = [float(e.get("value", 0)) for e in evts]
        return sum(vals) / len(vals) if vals else 0
    if agg_function == "max":
        return max((float(e.get("value", 0)) for e in evts), default=0)
    if agg_function == "min":
        return min((float(e.get("value", 0)) for e in evts), default=0)
    if agg_function == "uv":
        return len({e.get("user_id") for e in evts})
    return len(evts)


def _time_key(ts: int, time_grain: str) -> str:
    if time_grain == "minute":
        return f"{ts // 60 % 60:02d}m"
    if time_grain == "hour":
        return f"{ts // 3600 % 24:02d}:00"
    if time_grain == "day":
        return f"day_{ts // 86400}"
    if time_grain == "week":
        return f"week_{ts // 604800}"
    return "all"


def aggregate_events(config_id: str) -> AggResult:
    """执行事件聚合。"""
    config = store.find_by_field("event_aggregate", "config_id", config_id)
    if config is None:
        raise ValueError(f"Aggregate config not found: {config_id}")

    event_id = config["event_id"]
    agg_dimensions = list(config.get("agg_dimensions", []))
    agg_function = config["agg_function"]
    time_grain = config["time_grain"]
    filter_expr = config.get("filter_expr", "")
    delivered_values = config.get("delivered_values") or {}

    # 获取清洗产物作为输入（记录来源，便于核对链路）
    cleanse_artifacts = store.list_artifacts("event_cleanse")
    related_cleanse = None
    for ca in cleanse_artifacts:
        if ca.get("event_id") == event_id:
            related_cleanse = ca
            break

    events = _generate_cleaned_events(event_id)
    events = _apply_filter(events, filter_expr)
    events = _apply_delivery(events, delivered_values)

    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        key_parts = [str(e.get(dim, "unknown")) for dim in agg_dimensions]
        key_parts.append(_time_key(int(e.get("timestamp", 0)), time_grain))
        grouped[tuple(key_parts)].append(e)

    result_rows: list[dict[str, Any]] = []
    for key, evts in sorted(grouped.items()):
        row: dict[str, Any] = {dim: key[i] for i, dim in enumerate(agg_dimensions)}
        row["time_grain"] = key[-1]
        row["agg_value"] = _aggregate_value(agg_function, evts)
        result_rows.append(row)

    # 每个维度的覆盖范围：全集（属性字典/地区字典）与本次实际产出的值对比。
    coverage: dict[str, dict[str, Any]] = {}
    for dimension in agg_dimensions:
        universe = _dimension_universe(dimension)
        delivered_in_result = {str(row.get(dimension)) for row in result_rows}
        if universe is None:
            delivered = sorted(delivered_in_result)
            missing: list[str] = []
        else:
            delivered = [v for v in universe if v in delivered_in_result]
            missing = [v for v in universe if v not in delivered_in_result]
        coverage[dimension] = {
            "dimension": dimension,
            "universe": list(universe) if universe is not None else sorted(delivered_in_result),
            "delivered": delivered,
            "missing": missing,
            "complete": not missing,
        }

    result = AggResult(
        config_id=config_id,
        event_id=event_id,
        agg_dimensions=agg_dimensions,
        agg_function=agg_function,
        time_grain=time_grain,
        result_rows=result_rows,
        total_count=len(result_rows),
        coverage=coverage,
        output_table=config.get("output_table", ""),
    )

    payload = result.model_dump()
    payload["filter_expr"] = filter_expr
    payload["delivered_values"] = delivered_values
    payload["source_cleanse"] = related_cleanse.get("rule_id") if related_cleanse else None
    payload["input_event_count"] = len(events)

    # 存储产物：写入与版本号递增在仓库锁内完成，热力查询据此同步刷新。
    store.set_artifact("event_aggregate", config_id, payload)

    store.update("event_aggregate", config["id"], {
        "status": "已生效",
        "pending": False,
    })

    return result


def get_aggregate_result(config_id: str) -> dict[str, Any] | None:
    """获取聚合结果；没有产物时先补算一次。"""
    artifact = store.get_artifact("event_aggregate", config_id)
    if artifact:
        return artifact
    config = store.find_by_field("event_aggregate", "config_id", config_id)
    if config is None:
        return None
    try:
        result = aggregate_events(config_id)
        return result.model_dump()
    except ValueError:
        return None
