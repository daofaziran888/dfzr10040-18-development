<template>
  <section class="page" data-module="lineage_withdrawal">
    <header class="page-head">
      <div>
        <h2>来源血缘批量撤边</h2>
        <p class="page-desc">
          多选来源贡献一次提交撤边，逐条保留回执；指标调度凭回执领取失效/重算范围，
          报表通过分区快照导出取数时仍有资格的字段结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="seed-demo" @click="seedDemo">
          初始化下线A演示现场
        </button>
      </div>
    </header>

    <p v-if="message" class="page-desc" data-testid="flow-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="flow-error">{{ errorMessage }}</p>

    <h3>① 来源贡献（勾选后按来源批量撤除）</h3>
    <form class="filter-bar" @submit.prevent="reloadContributions">
      <label class="filter-item">
        <span>下线来源</span>
        <input v-model="withdrawSource" data-testid="withdraw-source" placeholder="如 A" />
      </label>
      <button class="btn primary" type="button" data-testid="submit-withdraw" @click="submitWithdraw">
        批量撤除勾选贡献
      </button>
      <button class="btn" type="button" data-testid="replay-withdraw" @click="replayWithdraw">
        重复提交同一撤除
      </button>
      <button class="btn ghost" type="button" data-testid="confirm-b" @click="confirmB">
        确认来源 B 已核实
      </button>
    </form>

    <table class="data-table" data-testid="contribution-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>贡献编号</th>
          <th>来源</th>
          <th>目标宽表</th>
          <th>目标字段</th>
          <th>来源状态</th>
          <th>血缘边状态</th>
          <th>撤边回执</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in contributions" :key="row.contribution_id" :data-status="row.血缘边状态">
          <td>
            <input
              v-model="selected"
              type="checkbox"
              :value="row.contribution_id"
              :data-testid="`pick-${row.contribution_id}`"
            />
          </td>
          <td>{{ row.contribution_id }}</td>
          <td>{{ row.来源 }}</td>
          <td>{{ row.目标宽表 }}</td>
          <td>{{ row.目标字段 }}</td>
          <td>{{ row.来源状态 }}</td>
          <td>{{ row.血缘边状态 }}</td>
          <td>{{ row.撤边回执 ?? '—' }}</td>
        </tr>
        <tr v-if="!contributions.length">
          <td colspan="8" class="empty-state">暂无来源贡献，请先初始化演示现场</td>
        </tr>
      </tbody>
    </table>

    <h3>② 指标调度领取实际失效 / 重算范围</h3>
    <div class="page-actions">
      <button class="btn primary" type="button" data-testid="claim-scope" @click="claimScope">
        调度领取一次
      </button>
      <span class="page-desc">最近领取：失效 {{ lastClaim.失效指标.length }} 项，重算 {{ lastClaim.重算指标.length }} 项</span>
    </div>
    <table v-if="claimEvents.length" class="data-table" data-testid="claim-table">
      <thead>
        <tr><th>事件编号</th><th>事件类型</th><th>指标</th><th>涉及来源</th><th>原因</th><th>来源回执</th></tr>
      </thead>
      <tbody>
        <tr v-for="event in claimEvents" :key="String(event.事件编号)">
          <td>{{ event.事件编号 }}</td>
          <td>{{ event.事件类型 }}</td>
          <td>{{ event.指标 }}</td>
          <td>{{ (event.涉及来源 as string[]).join('、') || '—' }}</td>
          <td>{{ event.原因 }}</td>
          <td>{{ event.来源回执 || '—' }}</td>
        </tr>
      </tbody>
    </table>

    <h3>③ 补货报表按分区快照导出</h3>
    <form class="filter-bar" @submit.prevent="runExport">
      <label class="filter-item">
        <span>快照编号</span>
        <input v-model="snapshotId" data-testid="snapshot-id" placeholder="如 SNAP-TODAY" />
      </label>
      <label class="filter-item">
        <span>导出模式</span>
        <select v-model="exportMode" data-testid="export-mode">
          <option value="with_blocked">含阻断字段与原因（补货核对）</option>
          <option value="eligible_only">仅导出仍有资格字段</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-export">导出当天分区</button>
    </form>
    <table v-if="exportRows.length" class="data-table" data-testid="export-table">
      <thead>
        <tr><th>字段</th><th>字段值</th><th>字段资格</th><th>快照资格</th><th>阻断原因</th><th>取数依据</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in exportRows" :key="row.字段" :data-status="row.快照资格">
          <td>{{ row.字段 }}</td>
          <td>{{ row.字段值 }}</td>
          <td>{{ row.字段资格 }}</td>
          <td>{{ row.快照资格 }}</td>
          <td>{{ row.阻断原因 || '—' }}</td>
          <td>{{ row.取数依据.join('、') }}</td>
        </tr>
      </tbody>
    </table>

    <h3>④ 撤边回执留证（逐条）</h3>
    <table class="data-table" data-testid="receipt-table">
      <thead>
        <tr><th>回执</th><th>贡献</th><th>来源</th><th>字段</th><th>撤除影响</th><th>字段资格</th><th>留存来源</th><th>原因</th></tr>
      </thead>
      <tbody>
        <tr v-for="receipt in receipts" :key="receipt.回执明细编号">
          <td>{{ receipt.撤边回执 }}</td>
          <td>{{ receipt.来源贡献 }}</td>
          <td>{{ receipt.来源 }}</td>
          <td>{{ receipt.目标字段 }}</td>
          <td>{{ receipt.撤除影响 }}</td>
          <td>{{ receipt.字段资格 }}</td>
          <td>{{ receipt.留存来源.join('、') || '—' }}</td>
          <td>{{ receipt.原因 }}</td>
        </tr>
        <tr v-if="!receipts.length">
          <td colspan="8" class="empty-state">尚未产生撤边回执</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  batchWithdraw,
  claimMetricScope,
  confirmSource,
  exportPartition,
  listContributions,
  listReceipts,
  publishSnapshot,
  registerContribution,
  type ClaimResult,
  type Contribution,
  type ExportRow,
  type ReceiptRow,
} from '@/api/lineage-withdrawal'

const TABLE = 'WT_REPLENISH'
const contributions = ref<Contribution[]>([])
const receipts = ref<ReceiptRow[]>([])
const selected = ref<string[]>([])
const withdrawSource = ref('A')
const snapshotId = ref('SNAP-TODAY')
const exportMode = ref('with_blocked')
const exportRows = ref<ExportRow[]>([])
const lastClaim = ref<ClaimResult>({
  ok: true, consumer: '', claimed_count: 0, 失效指标: [], 重算指标: [],
})
const claimEvents = ref<Record<string, unknown>[]>([])
const message = ref('')
const errorMessage = ref('')

async function reloadContributions() {
  const payload = await listContributions()
  contributions.value = payload.items
  const receiptPayload = await listReceipts()
  receipts.value = receiptPayload.items
}

async function seedDemo() {
  errorMessage.value = ''
  try {
    await Promise.all([
      registerContribution({ contribution_id: 'C-A-AMT', source: 'A', target_table: TABLE, target_field: '金额', source_status: '已确认' }),
      registerContribution({ contribution_id: 'C-B-AMT', source: 'B', target_table: TABLE, target_field: '金额', source_status: '待核' }),
      registerContribution({ contribution_id: 'C-A-INV', source: 'A', target_table: TABLE, target_field: '库存', source_status: '已确认' }),
      registerContribution({ contribution_id: 'C-C-QTY', source: 'C', target_table: TABLE, target_field: '销量', source_status: '已确认' }),
    ])
    await publishSnapshot({
      snapshot_id: 'SNAP-TODAY', target_table: TABLE, partition_value: '20261123',
      field_values: { 金额: 12000, 库存: 80, 销量: 300 },
    })
    message.value = '演示现场就绪：金额由 A（已确认）+ B（待核）支撑，库存仅 A 支撑，销量由 C 独立支撑。'
    await reloadContributions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '演示现场初始化失败'
  }
}

async function submitWithdraw() {
  errorMessage.value = ''
  if (!selected.value.length) {
    errorMessage.value = '请先勾选至少一条来源贡献'
    return
  }
  try {
    const result = await batchWithdraw(withdrawSource.value, selected.value)
    message.value = result.message
    selected.value = []
    await reloadContributions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量撤边失败'
  }
}

async function replayWithdraw() {
  errorMessage.value = ''
  try {
    const result = await batchWithdraw(withdrawSource.value, ['C-A-AMT', 'C-A-INV'], '重复提交同一撤除')
    message.value = result.message
    await reloadContributions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重复提交失败'
  }
}

async function confirmB() {
  errorMessage.value = ''
  try {
    const result = await confirmSource('B')
    message.value = result.message
    await reloadContributions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源确认失败'
  }
}

async function claimScope() {
  errorMessage.value = ''
  try {
    const result = await claimMetricScope('指标调度')
    lastClaim.value = result
    claimEvents.value = [...result.失效指标, ...result.重算指标]
    message.value = `调度领取完成：失效 ${result.失效指标.length} 项、重算 ${result.重算指标.length} 项（重复领取不会重复派发）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度领取失败'
  }
}

async function runExport() {
  errorMessage.value = ''
  try {
    const result = await exportPartition(snapshotId.value, exportMode.value, '补货报表')
    if (!result.ok) throw new Error('快照导出失败')
    exportRows.value = result.rows
    message.value = `分区导出 ${result.exported_rows} 个字段，资格与取数依据见表格`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区导出失败'
  }
}

onMounted(() => {
  void reloadContributions().catch((error: unknown) => {
    errorMessage.value = error instanceof Error ? error.message : '数据加载失败'
  })
})
</script>
