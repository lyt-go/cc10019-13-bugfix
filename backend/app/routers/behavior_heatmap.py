"""行为热力路由：按模板口径消费事件聚合结果，生成热力格并查询结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.behavior_heatmap import (
    METRIC_LABELS,
    generate_heatmap,
    get_heatmap,
    inspect_heatmap,
)
from app.store import store

router = APIRouter(prefix="/api/behavior-heatmap", tags=["behavior_heatmap"])

# 模板口径：热力视图只允许这两类口径，人数与次数不可混算。
TEMPLATES = [
    {"metric": "uv", "label": METRIC_LABELS["uv"], "description": "按用户去重，用于排查冷点"},
    {"metric": "count", "label": METRIC_LABELS["count"], "description": "按事件计数，用于观察访问量"},
]


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("behavior_heatmap")
    return {"items": rows, "total": len(rows)}


@router.get("/summary")
def summary() -> dict[str, Any]:
    """热力面板概览：模板口径、配置列表与各配置的可比/未覆盖地区。"""
    configs: list[dict[str, Any]] = []
    for row in store.rows("behavior_heatmap"):
        config_id = row.get("config_id")
        result = None
        if config_id:
            try:
                result = inspect_heatmap(config_id)
            except ValueError:
                result = None
        configs.append({
            "config_id": config_id,
            "event_id": row.get("event_id"),
            "spatial_dim": row.get("spatial_dim"),
            "metric": (result or {}).get("metric") or row.get("metric", ""),
            "source_aggregate": (result or {}).get("source_aggregate"),
            "source_revision": (result or {}).get("source_revision"),
            "covered_regions": (result or {}).get("covered_regions", []),
            "uncovered_regions": (result or {}).get("uncovered_regions", []),
            "status": row.get("status"),
        })

    # 概览指标按地区维度的配置统计，避免把页面维度也算进“地区”。
    region_configs = [c for c in configs if c["spatial_dim"] == "region"] or configs
    metrics = [
        {"label": "热力配置", "value": len(configs), "trend": 0},
        {"label": "可比地区", "value": len(region_configs[0]["covered_regions"]), "trend": 0},
        {"label": "未覆盖地区", "value": len(region_configs[0]["uncovered_regions"]), "trend": 0},
    ]
    return {"metrics": metrics, "templates": TEMPLATES, "configs": configs}


@router.post("/run")
def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """按面板参数重新生成热力格；config_id 省略时使用第一条配置。"""
    values = (payload or {}).get("values") or {}
    config_id = values.get("配置编号") or values.get("config_id")
    if not config_id:
        rows = store.rows("behavior_heatmap")
        if not rows:
            raise HTTPException(status_code=404, detail="No heatmap config")
        config_id = rows[0].get("config_id")

    metric = str(values.get("metric") or "").strip().lower() or None
    if metric is not None and metric not in METRIC_LABELS:
        raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")

    try:
        result = generate_heatmap(config_id, metric=metric)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    cells = result.get("cells", [])
    items = [
        {
            "地区": cell["dimension"],
            "时段": cell["time_slot"],
            "口径": cell["unit"],
            "格值": cell["value"],
            "色阶": cell["color"],
            "来源聚合": result.get("source_aggregate"),
        }
        for cell in cells
    ]
    return {
        "items": items,
        "total": len(items),
        "result": result,
    }


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("behavior_heatmap", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/generate")
def generate(config_id: str) -> ActionResult:
    try:
        result = generate_heatmap(config_id)
        return ActionResult(ok=True, message="Generated", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_heatmap(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("behavior_heatmap", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
