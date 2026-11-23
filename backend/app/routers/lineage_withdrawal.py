"""来源血缘批量撤边专用接口。

与通用资源路由分工：通用 /api/{module} 仍负责血缘边的查询/登记/单条动作，
本路由承载跨记录的批量撤边工作流（撤边回执、指标调度领取、分区快照导出）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.lineage_withdrawal import (
    EXPORT_WITH_BLOCKED,
    lineage_withdrawal_service as service,
)

router = APIRouter(prefix="/api/lineage-withdrawal", tags=["来源血缘撤边"])


class ContributionPayload(BaseModel):
    contribution_id: str
    source: str
    target_table: str
    target_field: str
    source_status: str = "待核"


class MetricPayload(BaseModel):
    metric: str
    target_table: str
    target_field: str
    agg_function: str = "sum"


class SnapshotPayload(BaseModel):
    snapshot_id: str
    target_table: str
    partition_value: str
    field_values: dict[str, Any]


class WithdrawPayload(BaseModel):
    source: str
    contribution_ids: list[str] = Field(default_factory=list)
    operator: str = "调度员"
    remark: str | None = None


class ClaimPayload(BaseModel):
    consumer: str


class ExportPayload(BaseModel):
    snapshot_id: str
    mode: str = EXPORT_WITH_BLOCKED
    consumer: str = "报表取数"


@router.post("/contributions")
def register_contribution(payload: ContributionPayload) -> dict[str, Any]:
    contribution = service.register_contribution(
        contribution_id=payload.contribution_id,
        source=payload.source,
        target_table=payload.target_table,
        target_field=payload.target_field,
        source_status=payload.source_status,
    )
    return {"ok": True, "message": "来源贡献已登记", "entry": contribution.to_dict()}


@router.post("/metrics")
def register_metric(payload: MetricPayload) -> dict[str, Any]:
    definition = service.register_metric(
        metric=payload.metric,
        target_table=payload.target_table,
        target_field=payload.target_field,
        agg_function=payload.agg_function,
    )
    return {
        "ok": True,
        "message": "指标定义已登记",
        "entry": {
            "metric": definition.metric,
            "target_table": definition.target_table,
            "target_field": definition.target_field,
            "agg_function": definition.agg_function,
        },
    }


@router.post("/snapshots")
def publish_snapshot(payload: SnapshotPayload) -> dict[str, Any]:
    snapshot = service.publish_snapshot(
        snapshot_id=payload.snapshot_id,
        target_table=payload.target_table,
        partition_value=payload.partition_value,
        field_values=payload.field_values,
    )
    return {"ok": True, "message": "分区快照已发布", "entry": snapshot.to_dict()}


@router.post("/withdrawals")
def batch_withdraw(payload: WithdrawPayload) -> dict[str, Any]:
    """多选来源贡献一次提交撤边，逐条返回撤边回执。"""
    return service.batch_withdraw(
        source=payload.source,
        contribution_ids=payload.contribution_ids,
        operator=payload.operator,
        remark=payload.remark or "",
    )


@router.post("/sources/{source}/confirm")
def confirm_source(source: str) -> dict[str, Any]:
    fields = service.confirm_source(source)
    return {
        "ok": True,
        "message": f"来源 {source} 已核实，受影响字段 {len(fields)} 个",
        "affected_fields": fields,
    }


@router.post("/claims")
def claim_metric_scope(payload: ClaimPayload) -> dict[str, Any]:
    """指标调度凭撤边回执领取一次实际失效或重算范围（领取动作幂等）。"""
    return service.claim_metric_scope(payload.consumer)


@router.post("/compute-gate")
def compute_gate(payload: MetricPayload) -> dict[str, Any]:
    """指标计算门禁：返回字段当前是否允许新计算及阻断原因。"""
    return service.compute_metric(payload.target_table, payload.target_field)


@router.post("/exports")
def export_partition(payload: ExportPayload) -> dict[str, Any]:
    """通过分区快照导出取数时仍有资格的字段结果与阻断原因。"""
    return service.export_partition(
        payload.snapshot_id, mode=payload.mode, consumer=payload.consumer,
    )


@router.get("/receipts")
def list_receipts() -> dict[str, Any]:
    items = service.list_receipts()
    return {"items": items, "total": len(items)}


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.get("/events")
def list_events() -> dict[str, Any]:
    items = service.list_events()
    return {"items": items, "total": len(items)}


@router.get("/contributions")
def list_contributions() -> dict[str, Any]:
    items = service.list_contributions()
    return {"items": items, "total": len(items)}


@router.get("/batches/{batch_id}/unaffected")
def unaffected_paths(batch_id: str) -> dict[str, Any]:
    items = service.unaffected_paths(batch_id)
    return {"items": items, "total": len(items)}
