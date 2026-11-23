/** 来源血缘批量撤边工作流接口封装。 */
import { request } from './client'

export type Contribution = {
  contribution_id: string
  来源: string
  目标宽表: string
  目标字段: string
  来源状态: string
  血缘边状态: string
  撤边回执: string | null
  撤边时间: string | null
  登记时间: string
}

export type ReceiptRow = {
  回执明细编号: string
  撤边回执: string
  来源贡献: string
  来源: string
  目标宽表: string
  目标字段: string
  撤除影响: string
  字段资格: string
  留存贡献: string[]
  留存来源: string[]
  原因: string
  留证时间: string
}

export type WithdrawalResult = {
  ok: boolean
  message: string
  duplicate_of_batch: string
  batch: {
    批次编号: string
    下线来源: string
    实际变更贡献: string[]
    是否重复提交: boolean
  }
  receipts: ReceiptRow[]
}

export type ClaimResult = {
  ok: boolean
  consumer: string
  claimed_count: number
  失效指标: Record<string, unknown>[]
  重算指标: Record<string, unknown>[]
}

export type ExportRow = {
  快照编号: string
  目标宽表: string
  分区: string
  字段: string
  字段值: string | number
  字段资格: string
  快照资格: string
  阻断原因: string
  取数依据: string[]
  取数时间: string
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}，操作未生效`)
  return (await response.json()) as T
}

export async function listContributions(): Promise<{ items: Contribution[] }> {
  const response = await request('/api/lineage-withdrawal/contributions')
  if (!response.ok) throw new Error('来源贡献读取失败')
  return await response.json()
}

export function registerContribution(payload: {
  contribution_id: string
  source: string
  target_table: string
  target_field: string
  source_status: string
}) {
  return postJson<{ ok: boolean }>('/api/lineage-withdrawal/contributions', payload)
}

export function publishSnapshot(payload: {
  snapshot_id: string
  target_table: string
  partition_value: string
  field_values: Record<string, number>
}) {
  return postJson<{ ok: boolean }>('/api/lineage-withdrawal/snapshots', payload)
}

export function batchWithdraw(source: string, contributionIds: string[], remark = '') {
  return postJson<WithdrawalResult>('/api/lineage-withdrawal/withdrawals', {
    source,
    contribution_ids: contributionIds,
    remark,
  })
}

export function confirmSource(source: string) {
  return postJson<{ ok: boolean; message: string; affected_fields: string[] }>(
    `/api/lineage-withdrawal/sources/${encodeURIComponent(source)}/confirm`,
    {},
  )
}

export function claimMetricScope(consumer: string) {
  return postJson<ClaimResult>('/api/lineage-withdrawal/claims', { consumer })
}

export function exportPartition(snapshotId: string, mode: string, consumer: string) {
  return postJson<{ ok: boolean; rows: ExportRow[]; exported_rows: number }>(
    '/api/lineage-withdrawal/exports',
    { snapshot_id: snapshotId, mode, consumer },
  )
}

export async function listReceipts(): Promise<{ items: ReceiptRow[] }> {
  const response = await request('/api/lineage-withdrawal/receipts')
  if (!response.ok) throw new Error('撤边回执读取失败')
  return await response.json()
}
