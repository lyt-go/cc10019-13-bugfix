"""事件聚合路由：执行事件聚合并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.event_aggregate import aggregate_events, get_aggregate_result
from app.store import store

router = APIRouter(prefix="/api/event-aggregate", tags=["event_aggregate"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("event_aggregate")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("event_aggregate")
    return {"items": rows, "total": len(rows)}


@router.get("/summary")
def summary() -> dict[str, Any]:
    """聚合面板概览：各配置的口径、覆盖范围与最近一次产物的版本。"""
    configs: list[dict[str, Any]] = []
    for row in store.rows("event_aggregate"):
        config_id = row.get("config_id", "")
        artifact = store.get_artifact("event_aggregate", config_id) or {}
        coverage = artifact.get("coverage") or {}
        configs.append({
            "config_id": config_id,
            "event_id": row.get("event_id"),
            "agg_function": row.get("agg_function"),
            "agg_dimensions": row.get("agg_dimensions", []),
            "time_grain": row.get("time_grain"),
            "status": row.get("status"),
            "revision": artifact.get("revision", 0),
            "coverage": coverage,
        })
    metrics = [
        {"label": "聚合配置", "value": len(configs), "trend": 0},
        {"label": "已产出", "value": sum(1 for c in configs if c["revision"]), "trend": 0},
        {"label": "待补算", "value": sum(1 for c in configs if not c["revision"]), "trend": 0},
    ]
    return {"metrics": metrics, "configs": configs}


@router.post("/run")
def run_entry(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """聚合入口重新提交：按 config_id 重算并返回最新产物。"""
    values = (payload or {}).get("values") or {}
    config_id = values.get("配置编号") or values.get("config_id")
    if not config_id:
        raise HTTPException(status_code=400, detail="config_id is required")
    try:
        aggregate_events(config_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    artifact = store.get_artifact("event_aggregate", config_id) or {}
    rows = [
        {
            "配置编号": artifact.get("config_id"),
            "事件编号": artifact.get("event_id"),
            "聚合函数": artifact.get("agg_function"),
            "维度值": " / ".join(str(r.get(d)) for d in artifact.get("agg_dimensions", [])),
            "聚合值": r.get("agg_value"),
            "时间粒度": r.get("time_grain"),
        }
        for r in artifact.get("result_rows", [])
    ]
    return {"items": rows, "total": len(rows), "result": artifact}


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("event_aggregate", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/run")
def run(config_id: str) -> ActionResult:
    try:
        result = aggregate_events(config_id)
        return ActionResult(ok=True, message="Aggregated", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_aggregate_result(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("event_aggregate", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
