import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  OverviewResult,
  PageResult,
  ReplayApproval,
  ReplayCredential,
  ReplayDelivery,
  ReplayExecutionResult,
  ReplayHandover,
  ReplayReceipt,
  TargetData,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 历史重放：凭据、转交、执行、回执与审批
// ---------------------------------------------------------------------------

type ItemList<T> = { items: T[]; total: number }

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error((detail && (detail.detail as string)) || fallback)
  }
  return (await response.json()) as T
}

export async function listReplayCredentials(
  archiveCode: string,
  status = '',
): Promise<ReplayCredential[]> {
  const query = new URLSearchParams({ archive_code: archiveCode })
  if (status) query.set('status', status)
  const response = await request(`/api/exchange_replay/credentials?${query}`)
  const payload = await readJson<ItemList<ReplayCredential>>(response, '重放凭据读取失败')
  return payload.items
}

export async function issueReplayCredential(input: {
  archive_code: string
  receiver: string
  members?: string[]
  one_time?: boolean
  note?: string
}): Promise<{ credential_code: string; members: string[]; member_details: EntryRow[] }> {
  const response = await request('/api/exchange_replay/credentials', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return await readJson(response, '重放凭据签发失败')
}

export async function listReplayHandovers(archiveCode: string): Promise<ReplayHandover[]> {
  const query = new URLSearchParams({ archive_code: archiveCode })
  const response = await request(`/api/exchange_replay/handovers?${query}`)
  const payload = await readJson<ItemList<ReplayHandover>>(response, '转交记录读取失败')
  return payload.items
}

export async function transferReplayBatch(input: {
  archive_code: string
  batch_code: string
  from_receiver: string
  to_receiver: string
  credential_code?: string
  revoke_old_credential?: boolean
  note?: string
}): Promise<ReplayHandover> {
  const response = await request('/api/exchange_replay/handovers', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return await readJson(response, '批次转交失败')
}

export async function createReplayTask(input: {
  archive_code: string
  replay_range: string
  mode?: string
  credential_code?: string
  target_code?: string
}): Promise<{ replay_id: number; replay_code: string }> {
  const response = await request('/api/exchange_replay/tasks', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return await readJson(response, '重放任务创建失败')
}

export async function executeReplayTask(replayId: number): Promise<ReplayExecutionResult> {
  const response = await request(`/api/exchange_replay/${replayId}/execute`, {
    method: 'POST',
  })
  return await readJson(response, '重放执行失败')
}

export async function loadReplayReceipt(receiptCode: string): Promise<ReplayReceipt> {
  const response = await request(
    `/api/exchange_replay/receipts/${encodeURIComponent(receiptCode)}`,
  )
  return await readJson(response, '交付回执读取失败')
}

export async function listReplayApprovals(
  replayCode: string,
  status = '待审批',
): Promise<ReplayApproval[]> {
  const query = new URLSearchParams({ replay_code: replayCode })
  if (status) query.set('status', status)
  const response = await request(`/api/exchange_replay/approvals?${query}`)
  const payload = await readJson<ItemList<ReplayApproval>>(response, '待审批事项读取失败')
  return payload.items
}

export async function loadReplayDelivery(replayId: number): Promise<ReplayDelivery> {
  const response = await request(`/api/exchange_replay/${replayId}/delivery`)
  return await readJson(response, '目标数据核对失败')
}

export async function loadTargetData(
  replayId: number,
  targetCode = '',
): Promise<TargetData> {
  const query = new URLSearchParams()
  if (targetCode) query.set('target_code', targetCode)
  const response = await request(
    `/api/exchange_replay/${replayId}/target-data?${query}`,
  )
  return await readJson(response, '目标数据读取失败')
}
