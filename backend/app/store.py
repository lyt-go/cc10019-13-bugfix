"""内存数据仓库：支持跨模块数据消费、版本快照和关联查询。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 同步锁：FastAPI 的同步路由运行在线程池里，聚合补算与热力查询可能并发。
        # 产物读写与版本号递增必须在同一把锁内完成，避免下游读到“半份”结果。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 数据产物存储：跨模块共享的数据结果
        self._artifacts: dict[str, dict[str, Any]] = {}
        # 产物版本号：每次写入自增，消费方用它判断是否需要重算派生结果
        self._artifact_revisions: dict[str, int] = {}
        # 版本快照
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 计算结果缓存
        self._compute_cache: dict[str, dict[str, Any]] = {}
        # 下一个自增ID
        self._next_ids: dict[str, int] = {
            name: max((int(row.get("id", 0)) for row in self._tables[name]), default=0) + 1
            for name in self._tables
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """插入一条记录并分配自增ID。"""
        if "id" not in row:
            row["id"] = self._next_ids.get(module, 1)
            self._next_ids[module] = row["id"] + 1
        self._tables.setdefault(module, []).append(dict(row))
        return row

    def update(self, module: str, entry_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """更新一条记录。"""
        row = self.find(module, entry_id)
        if row is None:
            return None
        row.update(updates)
        return row

    def delete(self, module: str, entry_id: int) -> bool:
        """删除一条记录。"""
        rows = self.rows(module)
        for i, row in enumerate(rows):
            if int(row.get("id", 0)) == entry_id:
                rows.pop(i)
                return True
        return False

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ========== 数据产物管理 ==========
    def set_artifact(self, artifact_type: str, artifact_id: str, data: dict[str, Any]) -> int:
        """存储跨模块数据产物，并返回写入后的版本号。

        写入与版本号递增在同一把锁内完成；同时把快照写进产物本身，
        并发读取时拿到的一定是一份完整且自洽的结果。
        """
        with self._lock:
            if artifact_type not in self._artifacts:
                self._artifacts[artifact_type] = {}
            revision = self._artifact_revisions.get(artifact_type, 0) + 1
            self._artifact_revisions[artifact_type] = revision
            stored = copy.deepcopy(data)
            stored.setdefault("revision", revision)
            self._artifacts[artifact_type][artifact_id] = stored
            return revision

    def get_artifact(self, artifact_type: str, artifact_id: str) -> dict[str, Any] | None:
        """获取跨模块数据产物（返回副本，避免调用方改坏已发布的版本）。"""
        with self._lock:
            artifact = self._artifacts.get(artifact_type, {}).get(artifact_id)
            return copy.deepcopy(artifact) if artifact is not None else None

    def list_artifacts(self, artifact_type: str) -> list[dict[str, Any]]:
        """列出某类型的所有产物（副本）。"""
        with self._lock:
            return copy.deepcopy(list(self._artifacts.get(artifact_type, {}).values()))

    def get_artifact_keys(self, artifact_type: str) -> list[str]:
        """获取某类型产物的所有键。"""
        with self._lock:
            return list(self._artifacts.get(artifact_type, {}).keys())

    def artifact_revision(self, artifact_type: str, artifact_id: str | None = None) -> int:
        """读取产物版本号；指定 artifact_id 时只认该产物的版本。"""
        with self._lock:
            if artifact_id is None:
                return self._artifact_revisions.get(artifact_type, 0)
            artifact = self._artifacts.get(artifact_type, {}).get(artifact_id)
            if artifact is None:
                return 0
            return int(artifact.get("revision", 0))

    # ========== 快照管理 ==========
    def add_snapshot(self, snapshot_type: str, data: dict[str, Any]) -> None:
        """添加快照。"""
        self._snapshots.setdefault(snapshot_type, []).append(data)

    def get_latest_snapshot(self, snapshot_type: str) -> dict[str, Any] | None:
        """获取最新快照。"""
        snapshots = self._snapshots.get(snapshot_type, [])
        return snapshots[-1] if snapshots else None

    def list_snapshots(self, snapshot_type: str) -> list[dict[str, Any]]:
        """列出所有快照。"""
        return list(self._snapshots.get(snapshot_type, []))

    # ========== 计算缓存 ==========
    def set_compute_cache(self, cache_key: str, data: dict[str, Any]) -> None:
        """存储计算缓存。"""
        self._compute_cache[cache_key] = data

    def get_compute_cache(self, cache_key: str) -> dict[str, Any] | None:
        """获取计算缓存。"""
        return self._compute_cache.get(cache_key)

    def invalidate_compute_cache(self, prefix: str) -> None:
        """按前缀失效缓存。"""
        keys_to_remove = [k for k in self._compute_cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._compute_cache[k]

    # ========== 跨模块关联查询 ==========
    def join(self, left_module: str, right_module: str, left_field: str, right_field: str) -> list[dict[str, Any]]:
        """执行简单的内存join。"""
        results: list[dict[str, Any]] = []
        left_rows = self.rows(left_module)
        right_rows = self.rows(right_module)
        right_index: dict[Any, list[dict[str, Any]]] = {}
        for r in right_rows:
            key = r.get(right_field)
            right_index.setdefault(key, []).append(r)
        for l in left_rows:
            key = l.get(left_field)
            for r in right_index.get(key, []):
                merged = dict(l)
                for rk, rv in r.items():
                    if rk not in merged:
                        merged[rk] = rv
                    else:
                        merged[f"{right_module}.{rk}"] = rv
                results.append(merged)
        return results


def query_filters(request: Any) -> dict[str, Any]:
    """从请求查询参数中提取过滤条件（排除分页字段）。"""
    params = dict(request.query_params)
    return {k: v for k, v in params.items() if k not in ("page", "size")}


store = Store()
