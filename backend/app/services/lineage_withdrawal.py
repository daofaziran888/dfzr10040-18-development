"""来源血缘批量撤边领域服务。

背景：来源血缘只会随宽表构建不断追加记录，缺少「按来源贡献批量撤除」的操作。
本模块提供一次提交多选来源贡献、逐条出具撤边回执的能力，并把撤边后的影响
分发给指标计算与分区快照报表两条下游链路：

- 指标调度凭撤边回执领取实际的失效或重算范围（先领一次、幂等再领）；
- 报表通过分区快照导出领取「取数时仍有资格」的字段结果，失去全部支持的
  字段只能以旧快照只读展示，并带阻断原因与取数依据。

关键规则（对应下线来源 A 的场景）：
- 库存仅由 A 支撑：撤边后没有任何有效来源，新计算阻断，旧发布值仅作可读依据；
- 金额由 A、B 共同支撑，B 尚未核实：撤 A 后金额不能被当成没有来源，进入待核，
  待 B 确认后仍可沿 B 聚合；
- 同一份撤除请求重复提交：库存只处理一次，已确认的 B 可让金额恢复资格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---- 领域常量：状态与影响类型集中定义，避免散落的魔法字符串 ----

# 来源映射核实状态
SOURCE_PENDING = "待核"
SOURCE_CONFIRMED = "已确认"

# 血缘边撤边后状态（与 lineage_edge 模块「已废弃」状态对齐）
EDGE_ACTIVE = "生效中"
EDGE_WITHDRAWN = "已废弃"

# 撤边回执对每个字段的实际影响
EFFECT_WITHDRAWN = "已撤除"        # 本次提交真正撤掉的有效贡献
EFFECT_DUPLICATE = "重复撤除"      # 同一贡献此前已撤，本次不重复处理
EFFECT_NOT_FOUND = "贡献不存在"    # 请求里选了一条系统找不到的贡献

# 撤边后字段的血缘资格
FIELD_ELIGIBLE = "有资格"          # 仍有已确认来源独立支撑
FIELD_PENDING = "待核"             # 来源还在但都未核实，不能当无来源处理
FIELD_BLOCKED = "已阻断"           # 失去全部有效来源，禁止按旧关系确认新计算

# 指标调度事件
EVENT_INVALIDATE = "指标失效"      # 字段无来源：指标只能失效
EVENT_RECOMPUTE = "重算待领"       # 字段仍有血缘：待核实后重算
EVENT_READY = "重算就绪"           # 待核来源被确认，重算范围可以领取

# 分区快照在报表中的资格
SNAPSHOT_ELIGIBLE = "有资格"
SNAPSHOT_READONLY = "只读留档"
SNAPSHOT_BLOCKED = "阻断"

# 报表导出模式
EXPORT_ELIGIBLE_ONLY = "eligible_only"   # 只导出取数时仍有资格的字段
EXPORT_WITH_BLOCKED = "with_blocked"     # 同时带回阻断字段与阻断原因（补货核对用）


def _now_iso() -> str:
    """统一时间戳，单测里只做包含性断言，不依赖固定时钟。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceContribution:
    """来源贡献：一条「来源 -> 目标宽表字段」的血缘边及其核实状态。"""
    contribution_id: str
    source: str
    target_table: str
    target_field: str
    source_status: str = SOURCE_PENDING
    edge_status: str = EDGE_ACTIVE
    created_at: str = field(default_factory=_now_iso)
    # 撤边溯源：哪一份回执把这条边撤掉的
    withdrawn_by_receipt: str | None = None
    withdrawn_at: str | None = None

    @property
    def active(self) -> bool:
        return self.edge_status == EDGE_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "来源": self.source,
            "目标宽表": self.target_table,
            "目标字段": self.target_field,
            "来源状态": self.source_status,
            "血缘边状态": self.edge_status,
            "撤边回执": self.withdrawn_by_receipt,
            "撤边时间": self.withdrawn_at,
            "登记时间": self.created_at,
        }


@dataclass
class FieldEffect:
    """单条贡献撤除的回执明细，逐条留证（一次提交 N 条贡献就有 N 条回执）。"""
    contribution_id: str
    source: str
    target_table: str
    target_field: str
    effect: str
    receipt_id: str
    # 撤除当下该字段剩余的有效贡献及其核实状态，用于说明撤除/保留范围
    remaining_contribution_ids: list[str]
    remaining_sources: list[str]
    field_eligibility: str
    reason: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "回执明细编号": f"{self.receipt_id}::{self.contribution_id}",
            "撤边回执": self.receipt_id,
            "来源贡献": self.contribution_id,
            "来源": self.source,
            "目标宽表": self.target_table,
            "目标字段": self.target_field,
            "撤除影响": self.effect,
            "字段资格": self.field_eligibility,
            "留存贡献": self.remaining_contribution_ids,
            "留存来源": self.remaining_sources,
            "原因": self.reason,
            "留证时间": self.recorded_at,
        }


@dataclass
class WithdrawalBatch:
    """一次批量撤除提交：请求指纹相同即视为同一份撤除（幂等依据）。"""
    batch_id: str
    request_key: str
    source: str
    contribution_ids: list[str]
    operator: str
    submitted_at: str
    receipts: list[FieldEffect]
    # 本次真正发生状态变化的贡献；重复提交时为空，保证库存只处理一次
    changed_contribution_ids: list[str]
    duplicate: bool
    remark: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "批次编号": self.batch_id,
            "请求指纹": self.request_key,
            "下线来源": self.source,
            "撤除贡献": self.contribution_ids,
            "操作人": self.operator,
            "是否重复提交": self.duplicate,
            "实际变更贡献": self.changed_contribution_ids,
            "提交时间": self.submitted_at,
            "备注": self.remark,
            "回执明细": [receipt.to_dict() for receipt in self.receipts],
        }


@dataclass
class MetricEvent:
    """指标调度事件：调度系统凭它领取实际的失效或重算范围。"""
    event_id: str
    event_type: str
    target_table: str
    target_field: str
    metric: str
    affected_sources: list[str]
    reason: str
    receipt_id: str
    batch_id: str
    created_at: str
    claimed_by: str | None = None
    claimed_at: str | None = None

    @property
    def claimed(self) -> bool:
        return self.claimed_by is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "事件编号": self.event_id,
            "事件类型": self.event_type,
            "目标宽表": self.target_table,
            "目标字段": self.target_field,
            "指标": self.metric,
            "涉及来源": self.affected_sources,
            "原因": self.reason,
            "来源回执": self.receipt_id,
            "撤边批次": self.batch_id,
            "产生时间": self.created_at,
            "领取状态": "已领取" if self.claimed else "待领取",
            "领取方": self.claimed_by,
            "领取时间": self.claimed_at,
        }


@dataclass
class PublishedSnapshot:
    """月内已发布的分区快照：旧发布值是当天排班补货的可读依据。"""
    snapshot_id: str
    target_table: str
    partition_value: str
    field_values: dict[str, Any]
    # 快照发布时各字段的取数依据（当时生效的来源贡献），导出时原样带回
    field_basis: dict[str, list[str]]
    published_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "快照编号": self.snapshot_id,
            "目标宽表": self.target_table,
            "分区": self.partition_value,
            "字段结果": dict(self.field_values),
            "发布时间": self.published_at,
        }


@dataclass
class MetricDefinition:
    """指标定义：字段 -> 指标名与聚合函数，失效/重算范围按指标留证。"""
    metric: str
    target_table: str
    target_field: str
    agg_function: str


class LineageWithdrawalService:
    """来源贡献登记、批量撤边、指标调度领取与分区快照导出的统一入口。

    所有状态都保存在实例内存中（与项目 store.py 的内存仓库约定一致），
    生产环境替换为数据库实现时，保持各方法的输入输出契约即可。
    """

    def __init__(self) -> None:
        self._contributions: dict[str, SourceContribution] = {}
        self._batches: dict[str, WithdrawalBatch] = {}
        self._receipts: dict[str, FieldEffect] = {}
        self._events: dict[str, MetricEvent] = {}
        self._snapshots: dict[str, PublishedSnapshot] = {}
        self._metrics: dict[str, MetricDefinition] = {}
        # 已处理的撤边请求指纹：重复提交直接走幂等分支
        self._request_keys: dict[str, str] = {}
        self._batch_seq = 0
        self._receipt_seq = 0
        self._event_seq = 0
        self._claim_seq = 0

    # ============ 1. 来源贡献与指标、快照登记 ============

    def register_contribution(
        self,
        contribution_id: str,
        source: str,
        target_table: str,
        target_field: str,
        source_status: str = SOURCE_PENDING,
    ) -> SourceContribution:
        """登记一条来源贡献（血缘边）。重复登记直接返回既有记录。"""
        existing = self._contributions.get(contribution_id)
        if existing is not None:
            return existing
        contribution = SourceContribution(
            contribution_id=contribution_id,
            source=source,
            target_table=target_table,
            target_field=target_field,
            source_status=source_status,
        )
        self._contributions[contribution_id] = contribution
        return contribution

    def confirm_source(self, source: str) -> list[str]:
        """核实来源：把该来源下所有有效贡献置为已确认。

        待核贡献被确认后，此前因撤边进入「待核」的字段恢复为有资格，
        并补发「重算就绪」事件，指标调度据此领取新的重算范围。
        返回受影响的字段标识（宽表.字段）。
        """
        affected_fields: set[str] = set()
        for contribution in self._contributions.values():
            if contribution.source == source and contribution.active:
                if contribution.source_status != SOURCE_CONFIRMED:
                    contribution.source_status = SOURCE_CONFIRMED
                    affected_fields.add(
                        f"{contribution.target_table}.{contribution.target_field}")
        for field_key in sorted(affected_fields):
            table, field_name = field_key.split(".", 1)
            eligibility, _ = self.evaluate_field(table, field_name)
            if eligibility == FIELD_ELIGIBLE:
                self._emit_event(
                    event_type=EVENT_READY,
                    table=table,
                    field_name=field_name,
                    sources=[source],
                    reason=f"来源 {source} 已核实，字段恢复已确认血缘支撑",
                    receipt_id="",
                    batch_id="",
                )
        return sorted(affected_fields)

    def register_metric(self, metric: str, target_table: str,
                        target_field: str, agg_function: str) -> MetricDefinition:
        """登记字段上的指标聚合定义。"""
        definition = MetricDefinition(
            metric=metric, target_table=target_table,
            target_field=target_field, agg_function=agg_function,
        )
        self._metrics[f"{target_table}.{target_field}"] = definition
        return definition

    def publish_snapshot(
        self,
        snapshot_id: str,
        target_table: str,
        partition_value: str,
        field_values: dict[str, Any],
    ) -> PublishedSnapshot:
        """发布分区快照，并固化发布时刻各字段的取数依据（生效贡献清单）。"""
        field_basis: dict[str, list[str]] = {}
        for field_name in field_values:
            field_basis[field_name] = sorted(
                contribution.contribution_id
                for contribution in self._contributions.values()
                if contribution.target_table == target_table
                and contribution.target_field == field_name
                and contribution.active
            )
        snapshot = PublishedSnapshot(
            snapshot_id=snapshot_id,
            target_table=target_table,
            partition_value=partition_value,
            field_values=dict(field_values),
            field_basis=field_basis,
            published_at=_now_iso(),
        )
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    # ============ 2. 批量撤边：多选来源贡献，一次提交，逐条回执 ============

    def batch_withdraw(
        self,
        source: str,
        contribution_ids: list[str],
        *,
        operator: str = "调度员",
        remark: str = "",
    ) -> dict[str, Any]:
        """按来源批量撤除贡献。

        - 同一来源 + 同一组贡献生成稳定请求指纹，重复提交不重复撤边；
        - 每条贡献出具独立回执（已撤除 / 重复撤除 / 贡献不存在）；
        - 只对真正发生变化的字段生成指标事件，保证库存只处理一次。
        """
        if not contribution_ids:
            return {"ok": False, "message": "撤边贡献清单为空，至少选择一条来源贡献"}
        unknown_source = sorted({
            cid for cid in contribution_ids
            if cid in self._contributions and self._contributions[cid].source != source
        })
        if unknown_source:
            return {
                "ok": False,
                "message": f"贡献 { '、'.join(unknown_source) } 不属于来源 {source}，撤除与保留范围冲突，已整单驳回",
            }

        request_key = self._build_request_key(source, contribution_ids)
        self._batch_seq += 1
        batch_id = f"WDRW-{self._batch_seq:04d}"
        is_duplicate = request_key in self._request_keys
        original_batch_id = self._request_keys.get(request_key, "")

        receipts: list[FieldEffect] = []
        changed: list[str] = []
        # 同一字段在本批次内只产生一个指标事件
        emitted_fields: set[str] = set()

        for contribution_id in contribution_ids:
            contribution = self._contributions.get(contribution_id)
            if contribution is None:
                receipt = self._build_receipt(
                    batch_id, contribution_id, source, "", "",
                    EFFECT_NOT_FOUND, "来源贡献不存在，未执行撤除",
                )
            elif not contribution.active:
                receipt = self._build_receipt(
                    batch_id, contribution_id, contribution.source,
                    contribution.target_table, contribution.target_field,
                    EFFECT_DUPLICATE,
                    f"该贡献已由回执 {contribution.withdrawn_by_receipt} 撤除，本次不重复处理",
                )
            else:
                contribution.edge_status = EDGE_WITHDRAWN
                contribution.withdrawn_by_receipt = f"RCPT-{self._receipt_seq + 1:04d}"
                contribution.withdrawn_at = _now_iso()
                changed.append(contribution_id)
                eligibility, reason = self.evaluate_field(
                    contribution.target_table, contribution.target_field)
                receipt = self._build_receipt(
                    batch_id, contribution_id, contribution.source,
                    contribution.target_table, contribution.target_field,
                    EFFECT_WITHDRAWN, reason,
                )
                field_key = f"{contribution.target_table}.{contribution.target_field}"
                if field_key not in emitted_fields:
                    emitted_fields.add(field_key)
                    self._emit_metric_event_for_field(
                        table=contribution.target_table,
                        field_name=contribution.target_field,
                        receipt_id=receipt.receipt_id,
                        batch_id=batch_id,
                    )
            receipts.append(receipt)
            self._receipts[receipt.receipt_id] = receipt

        batch = WithdrawalBatch(
            batch_id=batch_id,
            request_key=request_key,
            source=source,
            contribution_ids=list(contribution_ids),
            operator=operator,
            submitted_at=_now_iso(),
            receipts=receipts,
            changed_contribution_ids=changed,
            duplicate=is_duplicate,
            remark=remark,
        )
        self._batches[batch_id] = batch
        if not is_duplicate:
            self._request_keys[request_key] = batch_id

        return {
            "ok": True,
            "message": (
                f"来源 {source} 的 {len(contribution_ids)} 条贡献已逐条出具回执"
                + ("（重复提交，未产生新的撤边处理）" if is_duplicate else "")
            ),
            "batch": batch.to_dict(),
            "duplicate_of_batch": original_batch_id,
            "receipts": [receipt.to_dict() for receipt in receipts],
        }

    def evaluate_field(self, target_table: str,
                       target_field: str) -> tuple[str, str]:
        """评估字段在当前血缘下的资格与可读原因。"""
        active = [
            c for c in self._contributions.values()
            if c.target_table == target_table
            and c.target_field == target_field
            and c.active
        ]
        if not active:
            return FIELD_BLOCKED, "字段已失去全部有效来源贡献，禁止按旧血缘确认新计算"
        if all(c.source_status == SOURCE_CONFIRMED for c in active):
            sources = "、".join(sorted({c.source for c in active}))
            return FIELD_ELIGIBLE, f"仍有已确认来源 {sources} 独立支撑"
        pending = "、".join(sorted({c.source for c in active if c.source_status == SOURCE_PENDING}))
        return FIELD_PENDING, f"剩余来源 {pending} 尚在核实，字段保留血缘但暂不具备确认资格"

    # ============ 3. 指标调度：凭回执领取实际失效/重算范围 ============

    def claim_metric_scope(self, consumer: str) -> dict[str, Any]:
        """调度系统领取一次待处理事件；已领取事件不会被重复领取。"""
        pending_events = [event for event in self._events.values() if not event.claimed]
        claimed: list[MetricEvent] = []
        for event in pending_events:
            self._claim_seq += 1
            event.claimed_by = consumer
            event.claimed_at = _now_iso()
            claimed.append(event)

        invalidated = [e for e in claimed if e.event_type == EVENT_INVALIDATE]
        recompute = [e for e in claimed if e.event_type in (EVENT_RECOMPUTE, EVENT_READY)]
        return {
            "ok": True,
            "consumer": consumer,
            "claim_no": self._claim_seq,
            "claimed_count": len(claimed),
            "失效指标": [event.to_dict() for event in invalidated],
            "重算指标": [event.to_dict() for event in recompute],
            "events": [event.to_dict() for event in claimed],
        }

    def compute_metric(self, target_table: str, target_field: str) -> dict[str, Any]:
        """指标计算门禁：按字段当前血缘资格决定放行、待核还是阻断。"""
        eligibility, reason = self.evaluate_field(target_table, target_field)
        definition = self._metrics.get(f"{target_table}.{target_field}")
        metric = definition.metric if definition else f"{target_field}_指标"
        active_sources = sorted({
            c.source for c in self._contributions.values()
            if c.target_table == target_table
            and c.target_field == target_field
            and c.active
        })
        if eligibility == FIELD_BLOCKED:
            return {
                "ok": False,
                "metric": metric,
                "资格": eligibility,
                "阻断原因": reason,
                "可用来源": active_sources,
            }
        if eligibility == FIELD_PENDING:
            return {
                "ok": False,
                "metric": metric,
                "资格": eligibility,
                "阻断原因": reason,
                "可用来源": active_sources,
            }
        return {
            "ok": True,
            "metric": metric,
            "资格": eligibility,
            "聚合来源": active_sources,
            "agg_function": definition.agg_function if definition else "sum",
            "message": f"{metric} 可沿已确认来源 { '、'.join(active_sources) } 聚合",
        }

    # ============ 4. 分区快照导出：领取取数时仍有资格的结果 ============

    def export_partition(
        self,
        snapshot_id: str,
        *,
        mode: str = EXPORT_WITH_BLOCKED,
        consumer: str = "报表取数",
    ) -> dict[str, Any]:
        """按分区快照导出字段结果，逐字段标注取数时刻的资格与依据。

        - eligible_only：只返回仍有资格的字段；
        - with_blocked：阻断字段一并返回，值取自旧快照并标记只读/阻断，
          同时给出阻断原因，满足补货报表「核对库存数或阻断原因」的诉求。
        """
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"分区快照 {snapshot_id} 不存在"}

        rows: list[dict[str, Any]] = []
        for field_name, value in snapshot.field_values.items():
            eligibility, reason = self.evaluate_field(
                snapshot.target_table, field_name)
            if eligibility == FIELD_ELIGIBLE:
                snapshot_status = SNAPSHOT_ELIGIBLE
                block_reason = ""
            elif eligibility == FIELD_PENDING:
                # 待核不是无来源：旧值只读留档，新计算等待核实结果
                snapshot_status = SNAPSHOT_READONLY
                block_reason = reason
            else:
                snapshot_status = SNAPSHOT_BLOCKED
                block_reason = reason
            if mode == EXPORT_ELIGIBLE_ONLY and eligibility != FIELD_ELIGIBLE:
                continue
            rows.append({
                "快照编号": snapshot.snapshot_id,
                "目标宽表": snapshot.target_table,
                "分区": snapshot.partition_value,
                "字段": field_name,
                "字段值": value,
                "字段资格": eligibility,
                "快照资格": snapshot_status,
                "阻断原因": block_reason,
                "取数依据": list(snapshot.field_basis.get(field_name, [])),
                "发布时间": snapshot.published_at,
                "取数方": consumer,
                "取数时间": _now_iso(),
            })
        return {
            "ok": True,
            "snapshot": snapshot.to_dict(),
            "mode": mode,
            "exported_rows": len(rows),
            "rows": rows,
        }

    # ============ 5. 留证查询：待核、已确认变更、未受影响路径 ============

    def list_receipts(self) -> list[dict[str, Any]]:
        return [receipt.to_dict() for receipt in self._receipts.values()]

    def list_batches(self) -> list[dict[str, Any]]:
        return [batch.to_dict() for batch in self._batches.values()]

    def list_events(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events.values()]

    def list_contributions(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._contributions.values()]

    def unaffected_paths(self, batch_id: str) -> list[dict[str, Any]]:
        """提取某批次未受影响的血缘路径（重复撤除与本就不在请求内的贡献）。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return []
        requested = set(batch.contribution_ids)
        result: list[dict[str, Any]] = []
        for receipt in batch.receipts:
            if receipt.effect in (EFFECT_DUPLICATE, EFFECT_NOT_FOUND):
                result.append(receipt.to_dict())
        for contribution in self._contributions.values():
            if contribution.contribution_id not in requested and contribution.active:
                result.append({
                    "回执明细编号": None,
                    "撤边批次": batch_id,
                    "来源贡献": contribution.contribution_id,
                    "来源": contribution.source,
                    "目标宽表": contribution.target_table,
                    "目标字段": contribution.target_field,
                    "撤除影响": "未受影响",
                    "字段资格": self.evaluate_field(
                        contribution.target_table, contribution.target_field)[0],
                    "留存贡献": [contribution.contribution_id],
                    "留存来源": [contribution.source],
                    "原因": "不在本次撤除清单内，血缘保持独立支撑",
                })
        return result

    # ============ 内部辅助 ============

    def _build_receipt(
        self, batch_id: str, contribution_id: str, source: str,
        target_table: str, target_field: str, effect: str, reason: str,
    ) -> FieldEffect:
        self._receipt_seq += 1
        active = [
            c for c in self._contributions.values()
            if target_table
            and c.target_table == target_table
            and c.target_field == target_field
            and c.active
        ]
        eligibility = (
            self.evaluate_field(target_table, target_field)[0]
            if target_table else "不适用"
        )
        return FieldEffect(
            contribution_id=contribution_id,
            source=source,
            target_table=target_table,
            target_field=target_field,
            effect=effect,
            receipt_id=f"RCPT-{self._receipt_seq:04d}",
            remaining_contribution_ids=[c.contribution_id for c in active],
            remaining_sources=sorted({c.source for c in active}),
            field_eligibility=eligibility,
            reason=reason,
            recorded_at=_now_iso(),
        )

    def _emit_metric_event_for_field(
        self, *, table: str, field_name: str,
        receipt_id: str, batch_id: str,
    ) -> None:
        eligibility, reason = self.evaluate_field(table, field_name)
        active_sources = sorted({
            c.source for c in self._contributions.values()
            if c.target_table == table and c.target_field == field_name and c.active
        })
        definition = self._metrics.get(f"{table}.{field_name}")
        metric = definition.metric if definition else f"{field_name}_指标"
        if eligibility == FIELD_BLOCKED:
            event_type = EVENT_INVALIDATE
            reason = f"{metric} 失去最后一条来源支撑：{reason}"
        else:
            event_type = EVENT_RECOMPUTE
            reason = f"{metric} 撤除后仍有血缘（{reason}），列入重算范围"
        self._emit_event(
            event_type=event_type, table=table, field_name=field_name,
            sources=active_sources, reason=reason,
            receipt_id=receipt_id, batch_id=batch_id, metric=metric,
        )

    def _emit_event(
        self, *, event_type: str, table: str, field_name: str,
        sources: list[str], reason: str, receipt_id: str, batch_id: str,
        metric: str | None = None,
    ) -> None:
        if metric is None:
            definition = self._metrics.get(f"{table}.{field_name}")
            metric = definition.metric if definition else f"{field_name}_指标"
        self._event_seq += 1
        self._events[f"EVT-{self._event_seq:04d}"] = MetricEvent(
            event_id=f"EVT-{self._event_seq:04d}",
            event_type=event_type,
            target_table=table,
            target_field=field_name,
            metric=metric,
            affected_sources=sources,
            reason=reason,
            receipt_id=receipt_id,
            batch_id=batch_id,
            created_at=_now_iso(),
        )

    @staticmethod
    def _build_request_key(source: str, contribution_ids: list[str]) -> str:
        """请求指纹：来源 + 排序后的贡献清单，保证同单重放可识别。"""
        return f"{source}||{','.join(sorted(contribution_ids))}"


# 全局单例，与 fusion_engine / store 的单例约定保持一致
lineage_withdrawal_service = LineageWithdrawalService()
