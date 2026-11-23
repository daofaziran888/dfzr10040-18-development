"""来源血缘批量撤边测试。

完整复刻「下线来源 A」的业务叙事：
- A 同时支撑金额、库存；B 对金额的独立映射还在核对；C 对销量的映射不受影响；
- 一次提交多选 A 的两份贡献，逐条出具撤边回执；
- 库存失去最后一条支持 -> 指标失效、新计算阻断、旧快照只读留档；
- 金额还有待核的 B -> 不能被当成没有来源，进入重算待领；
- 指标调度先领一次实际失效/重算范围，再从补货报表导出当天分区核对；
- B 确认后重复提交同一撤除：库存只处理一次，金额可沿 B 聚合。
"""
from __future__ import annotations

import pytest

from app.services.lineage_withdrawal import (
    EDGE_ACTIVE,
    EDGE_WITHDRAWN,
    EFFECT_DUPLICATE,
    EFFECT_NOT_FOUND,
    EFFECT_WITHDRAWN,
    EVENT_INVALIDATE,
    EVENT_READY,
    EVENT_RECOMPUTE,
    EXPORT_ELIGIBLE_ONLY,
    FIELD_BLOCKED,
    FIELD_ELIGIBLE,
    FIELD_PENDING,
    SNAPSHOT_BLOCKED,
    SNAPSHOT_ELIGIBLE,
    SNAPSHOT_READONLY,
    SOURCE_CONFIRMED,
    SOURCE_PENDING,
    LineageWithdrawalService,
)

# TestClient 依赖 httpx；锁定依赖未包含时仅跳过 HTTP 冒烟，领域测试不受影响。
try:
    from fastapi.testclient import TestClient
except (RuntimeError, ImportError):  # pragma: no cover - 取决于环境是否安装 httpx
    TestClient = None

TABLE = "WT_REPLENISH"  # 补货宽表
PARTITION = "20261123"  # 当天分区


@pytest.fixture
def svc() -> LineageWithdrawalService:
    """构造撤边前的血缘现场：金额(A+B，B待核)、库存(仅A)、销量(仅C，已确认)。"""
    service = LineageWithdrawalService()
    service.register_contribution("C-A-AMT", "A", TABLE, "金额", SOURCE_CONFIRMED)
    service.register_contribution("C-B-AMT", "B", TABLE, "金额", SOURCE_PENDING)
    service.register_contribution("C-A-INV", "A", TABLE, "库存", SOURCE_CONFIRMED)
    service.register_contribution("C-C-QTY", "C", TABLE, "销量", SOURCE_CONFIRMED)
    service.register_metric("金额指标", TABLE, "金额", "sum")
    service.register_metric("库存指标", TABLE, "库存", "sum")
    service.register_metric("销量指标", TABLE, "销量", "sum")
    # 月内已发布的分区快照：当天排班补货的可读依据
    service.publish_snapshot(
        "SNAP-TODAY", TABLE, PARTITION,
        {"金额": 12000, "库存": 80, "销量": 300},
    )
    return service


class TestBatchWithdrawalReceipts:
    """多选来源贡献一次提交，逐条保留撤边回执。"""

    def test_batch_withdraw_emits_one_receipt_per_contribution(
        self, svc: LineageWithdrawalService,
    ) -> None:
        result = svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"], operator="值班调度")
        assert result["ok"] is True
        receipts = result["receipts"]
        assert len(receipts) == 2
        assert {r["来源贡献"] for r in receipts} == {"C-A-AMT", "C-A-INV"}
        assert all(r["撤除影响"] == EFFECT_WITHDRAWN for r in receipts)
        # 回执编号逐条独立、可追溯
        assert len({r["撤边回执"] for r in receipts}) == 2

    def test_withdrawn_edges_are_marked_discarded_but_kept(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        amt = svc._contributions["C-A-AMT"]
        inv = svc._contributions["C-A-INV"]
        assert amt.edge_status == EDGE_WITHDRAWN and inv.edge_status == EDGE_WITHDRAWN
        assert amt.withdrawn_by_receipt and inv.withdrawn_by_receipt
        # 撤边是留证操作，记录本身不删除
        assert set(svc._contributions) == {"C-A-AMT", "C-B-AMT", "C-A-INV", "C-C-QTY"}

    def test_conflict_scope_rejects_contribution_from_other_source(
        self, svc: LineageWithdrawalService,
    ) -> None:
        # B 的贡献混进 A 的下线单：撤除与保留范围冲突，整单驳回
        result = svc.batch_withdraw("A", ["C-A-INV", "C-B-AMT"])
        assert result["ok"] is False
        assert "冲突" in result["message"]
        # 驳回后没有任何边被撤
        assert svc._contributions["C-A-INV"].edge_status == EDGE_ACTIVE
        assert svc._contributions["C-B-AMT"].edge_status == EDGE_ACTIVE

    def test_empty_contribution_list_rejected(self, svc: LineageWithdrawalService) -> None:
        result = svc.batch_withdraw("A", [])
        assert result["ok"] is False

    def test_unknown_contribution_gets_not_found_receipt(
        self, svc: LineageWithdrawalService,
    ) -> None:
        result = svc.batch_withdraw("A", ["C-A-INV", "C-GHOST"])
        receipts = {r["来源贡献"]: r for r in result["receipts"]}
        assert receipts["C-GHOST"]["撤除影响"] == EFFECT_NOT_FOUND
        assert receipts["C-A-INV"]["撤除影响"] == EFFECT_WITHDRAWN


class TestFieldEligibilityAfterWithdrawal:
    """撤边后字段资格：库存阻断、金额待核（不能当无来源）、销量未受影响。"""

    def test_inventory_blocked_after_losing_last_support(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        eligibility, reason = svc.evaluate_field(TABLE, "库存")
        assert eligibility == FIELD_BLOCKED
        assert "全部有效来源" in reason

    def test_amount_pending_not_sourceless_while_b_unverified(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        eligibility, reason = svc.evaluate_field(TABLE, "金额")
        # B 还在核对：金额有血缘但待核，绝不能被当成没有任何来源
        assert eligibility == FIELD_PENDING
        assert "B" in reason
        amt_receipt = next(
            r for r in svc.list_receipts() if r["来源贡献"] == "C-A-AMT")
        assert amt_receipt["留存来源"] == ["B"]
        assert amt_receipt["留存贡献"] == ["C-B-AMT"]

    def test_unrelated_path_remains_eligible(
        self, svc: LineageWithdrawalService,
    ) -> None:
        result = svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        # C 对销量的独立映射保持支撑
        eligibility, _ = svc.evaluate_field(TABLE, "销量")
        assert eligibility == FIELD_ELIGIBLE
        unaffected = svc.unaffected_paths(result["batch"]["批次编号"])
        paths = {item["来源贡献"]: item for item in unaffected}
        assert "C-C-QTY" in paths
        assert paths["C-C-QTY"]["撤除影响"] == "未受影响"

    def test_compute_gate_blocks_new_calculation_with_old_relation(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        inv = svc.compute_metric(TABLE, "库存")
        amt = svc.compute_metric(TABLE, "金额")
        assert inv["ok"] is False and inv["资格"] == FIELD_BLOCKED
        assert amt["ok"] is False and amt["资格"] == FIELD_PENDING
        # 待核金额仍能看到来源 B，不是空来源
        assert amt["可用来源"] == ["B"]


class TestMetricSchedulerClaim:
    """指标调度凭撤边回执领取实际失效或重算范围。"""

    def test_first_claim_receives_invalidate_and_recompute_scopes(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        claim = svc.claim_metric_scope("指标调度")
        assert claim["claimed_count"] == 2
        invalidated = claim["失效指标"]
        recompute = claim["重算指标"]
        assert len(invalidated) == 1 and invalidated[0]["指标"] == "库存指标"
        assert invalidated[0]["事件类型"] == EVENT_INVALIDATE
        assert len(recompute) == 1 and recompute[0]["指标"] == "金额指标"
        assert recompute[0]["事件类型"] == EVENT_RECOMPUTE
        # 每条事件都可回溯到撤边回执
        assert all(event["来源回执"].startswith("RCPT-") for event in claim["events"])

    def test_claim_is_idempotent_second_claim_gets_nothing(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        first = svc.claim_metric_scope("指标调度")
        second = svc.claim_metric_scope("指标调度")
        assert first["claimed_count"] == 2
        assert second["claimed_count"] == 0
        assert second["失效指标"] == [] and second["重算指标"] == []

    def test_duplicate_withdrawal_processes_inventory_only_once(
        self, svc: LineageWithdrawalService,
    ) -> None:
        first = svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        svc.claim_metric_scope("指标调度")
        second = svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        # 同一请求指纹：重复提交，没有新的实际变更
        assert second["duplicate_of_batch"] == first["batch"]["批次编号"]
        assert second["batch"]["实际变更贡献"] == []
        assert all(r["撤除影响"] == EFFECT_DUPLICATE for r in second["receipts"])
        # 不会再产生调度事件，库存只处理一次
        assert svc.claim_metric_scope("指标调度")["claimed_count"] == 0
        assert svc._contributions["C-A-INV"].withdrawn_at is not None


class TestPartitionSnapshotExport:
    """报表通过分区快照导出领取取数时仍有资格的金额和库存结果。"""

    def test_export_with_blocked_keeps_readable_basis_and_reason(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        export = svc.export_partition("SNAP-TODAY", consumer="补货报表")
        assert export["ok"] is True
        rows = {row["字段"]: row for row in export["rows"]}
        # 库存：旧发布值仍可读，但标记阻断并给出原因与取数依据
        assert rows["库存"]["字段值"] == 80
        assert rows["库存"]["快照资格"] == SNAPSHOT_BLOCKED
        assert rows["库存"]["字段资格"] == FIELD_BLOCKED
        assert "全部有效来源" in rows["库存"]["阻断原因"]
        assert rows["库存"]["取数依据"] == ["C-A-INV"]
        # 金额：待核，旧值只读留档
        assert rows["金额"]["字段值"] == 12000
        assert rows["金额"]["快照资格"] == SNAPSHOT_READONLY
        assert rows["金额"]["字段资格"] == FIELD_PENDING
        # 销量：未受影响，仍有资格
        assert rows["销量"]["快照资格"] == SNAPSHOT_ELIGIBLE
        assert rows["销量"]["字段资格"] == FIELD_ELIGIBLE

    def test_eligible_only_export_drops_blocked_and_pending(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        export = svc.export_partition("SNAP-TODAY", mode=EXPORT_ELIGIBLE_ONLY)
        fields = {row["字段"] for row in export["rows"]}
        assert fields == {"销量"}

    def test_export_unknown_snapshot_fails(self, svc: LineageWithdrawalService) -> None:
        result = svc.export_partition("SNAP-NOPE")
        assert result["ok"] is False
        assert "不存在" in result["message"]


class TestConfirmSourceAndReplay:
    """确认 B 后重复同一撤除：库存只处理一次，金额仍可沿 B 聚合。"""

    def test_confirm_b_restores_amount_and_emits_ready_event(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        svc.claim_metric_scope("指标调度")
        affected = svc.confirm_source("B")
        assert affected == [f"{TABLE}.金额"]
        assert svc._contributions["C-B-AMT"].source_status == SOURCE_CONFIRMED
        eligibility, _ = svc.evaluate_field(TABLE, "金额")
        assert eligibility == FIELD_ELIGIBLE
        # 新的重算就绪事件可供调度领取
        claim = svc.claim_metric_scope("指标调度")
        assert len(claim["重算指标"]) == 1
        assert claim["重算指标"][0]["事件类型"] == EVENT_READY
        assert claim["重算指标"][0]["指标"] == "金额指标"
        # 库存没有新事件：仍然只失效一次
        assert claim["失效指标"] == []

    def test_amount_aggregates_along_b_after_confirmation(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        svc.confirm_source("B")
        gate = svc.compute_metric(TABLE, "金额")
        assert gate["ok"] is True
        assert gate["聚合来源"] == ["B"]
        # 库存依旧阻断，不会因 B 确认而恢复
        inv = svc.compute_metric(TABLE, "库存")
        assert inv["ok"] is False and inv["资格"] == FIELD_BLOCKED

    def test_replay_same_withdrawal_after_confirmation(
        self, svc: LineageWithdrawalService,
    ) -> None:
        svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        svc.confirm_source("B")
        replay = svc.batch_withdraw("A", ["C-A-AMT", "C-A-INV"])
        assert replay["batch"]["实际变更贡献"] == []
        assert all(r["撤除影响"] == EFFECT_DUPLICATE for r in replay["receipts"])
        # 金额沿 B 仍可聚合；库存维持阻断且只处理过一次
        assert svc.compute_metric(TABLE, "金额")["ok"] is True
        assert svc.compute_metric(TABLE, "库存")["资格"] == FIELD_BLOCKED
        # 快照导出：金额恢复有资格，库存仍是阻断的只读旧值
        export = svc.export_partition("SNAP-TODAY", consumer="补货报表复核")
        rows = {row["字段"]: row for row in export["rows"]}
        assert rows["金额"]["快照资格"] == SNAPSHOT_ELIGIBLE
        assert rows["库存"]["快照资格"] == SNAPSHOT_BLOCKED
        assert rows["销量"]["快照资格"] == SNAPSHOT_ELIGIBLE


class TestWithdrawalApi:
    """HTTP 层冒烟：路由注册、契约与动作接口 {ok, message} 约定。"""

    @pytest.fixture
    def client(self):
        if TestClient is None:
            pytest.skip("环境缺少 httpx，TestClient 不可用")
        from app.main import app

        return TestClient(app)

    def test_full_workflow_over_http(self, client: TestClient) -> None:
        headers = {"Content-Type": "application/json"}
        for payload in (
            {"contribution_id": "C-A-AMT", "source": "A", "target_table": TABLE,
             "target_field": "金额", "source_status": SOURCE_CONFIRMED},
            {"contribution_id": "C-B-AMT", "source": "B", "target_table": TABLE,
             "target_field": "金额", "source_status": SOURCE_PENDING},
            {"contribution_id": "C-A-INV", "source": "A", "target_table": TABLE,
             "target_field": "库存", "source_status": SOURCE_CONFIRMED},
        ):
            resp = client.post("/api/lineage-withdrawal/contributions", json=payload, headers=headers)
            assert resp.status_code == 200 and resp.json()["ok"] is True

        snap = client.post("/api/lineage-withdrawal/snapshots", json={
            "snapshot_id": "SNAP-HTTP", "target_table": TABLE,
            "partition_value": PARTITION, "field_values": {"金额": 500, "库存": 9},
        }, headers=headers)
        assert snap.json()["ok"] is True

        withdraw = client.post("/api/lineage-withdrawal/withdrawals", json={
            "source": "A", "contribution_ids": ["C-A-AMT", "C-A-INV"],
        }, headers=headers).json()
        assert withdraw["ok"] is True and len(withdraw["receipts"]) == 2

        claim = client.post("/api/lineage-withdrawal/claims",
                            json={"consumer": "指标调度"}, headers=headers).json()
        assert claim["claimed_count"] == 2

        report = client.post("/api/lineage-withdrawal/exports", json={
            "snapshot_id": "SNAP-HTTP", "consumer": "补货报表",
        }, headers=headers).json()
        statuses = {row["字段"]: row["快照资格"] for row in report["rows"]}
        assert statuses == {"金额": SNAPSHOT_READONLY, "库存": SNAPSHOT_BLOCKED}

        client.post("/api/lineage-withdrawal/sources/B/confirm", headers=headers)
        replay = client.post("/api/lineage-withdrawal/withdrawals", json={
            "source": "A", "contribution_ids": ["C-A-AMT", "C-A-INV"],
        }, headers=headers).json()
        assert replay["batch"]["实际变更贡献"] == []

        receipts = client.get("/api/lineage-withdrawal/receipts").json()
        assert receipts["total"] == 4  # 首次 2 条 + 重放 2 条逐条回执
