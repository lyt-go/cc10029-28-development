<template>
  <section class="replay-panel" data-testid="replay-panel">
    <article class="replay-card">
      <h3>重放可回取范围凭据</h3>
      <p class="page-desc">
        凭据按归档卷成员批次签发，逐条登记源端血缘、当时归属接收方与一次性回取资格。
      </p>
      <div class="replay-grid">
        <label>
          <span>归档编号</span>
          <input v-model="archiveCode" data-testid="replay-archive" placeholder="HIST-..." />
        </label>
        <label>
          <span>签发对象（接收方）</span>
          <input v-model="receiver" data-testid="replay-receiver" placeholder="TARG-..." />
        </label>
        <label>
          <span>成员批次（留空为整卷）</span>
          <input v-model="memberInput" data-testid="replay-members" placeholder="批次编号，用逗号分隔" />
        </label>
        <label class="replay-inline">
          <span><input v-model="oneTime" type="checkbox" /> 一次性回取</span>
        </label>
        <button class="btn primary" data-testid="replay-issue" type="button" @click="issue">
          签发凭据
        </button>
        <button class="btn" data-testid="replay-refresh" type="button" @click="refreshAll">
          刷新凭据
        </button>
      </div>

      <table v-if="credentials.length" class="replay-table">
        <thead>
          <tr>
            <th>凭据编号</th><th>签发对象</th><th>状态</th><th>一次性</th>
            <th>成员批次 / 源端</th><th>已回取</th><th>未回取</th><th>签发时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="credential in credentials" :key="credential.凭据编号">
            <td>{{ credential.凭据编号 }}</td>
            <td>{{ credential.签发对象 }}</td>
            <td><span class="replay-tag" :class="credentialTone(credential.状态)">{{ credential.状态 }}</span></td>
            <td>{{ credential.一次性 ? '是' : '否' }}</td>
            <td>
              <div v-for="member in credential.成员明细" :key="member.批次编号">
                {{ member.批次编号 }} · {{ member.源端编号 || '—' }}
                <span class="replay-tag" :class="member.成员状态 === '已回取' ? 'ok' : 'warn'">
                  {{ member.成员状态 }}
                </span>
              </div>
            </td>
            <td>{{ listText(credential.已回取成员) }}</td>
            <td>{{ listText(credential.未回取成员) }}</td>
            <td>{{ credential.签发时间文本 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="page-desc">暂无凭据记录，填写归档编号与接收方后签发。</p>
    </article>

    <article class="replay-card">
      <h3>卷内批次转交</h3>
      <p class="page-desc">
        转交只变更批次归属，不会顺手撤销旧任务凭据；交接前签出的进行中任务仍可按原凭据执行未转交成员。
      </p>
      <div class="replay-grid">
        <label>
          <span>批次编号</span>
          <input v-model="handoverBatch" data-testid="handover-batch" />
        </label>
        <label>
          <span>原接收方</span>
          <input v-model="handoverFrom" data-testid="handover-from" />
        </label>
        <label>
          <span>新接收方</span>
          <input v-model="handoverTo" data-testid="handover-to" />
        </label>
        <label class="replay-inline">
          <span><input v-model="revokeOld" type="checkbox" /> 同时撤销旧任务凭据</span>
        </label>
        <button class="btn" data-testid="handover-submit" type="button" @click="handover">
          登记转交
        </button>
      </div>
      <table v-if="handovers.length" class="replay-table">
        <thead>
          <tr>
            <th>交接编号</th><th>批次编号</th><th>原接收方</th><th>新接收方</th>
            <th>旧任务凭据</th><th>旧凭据已撤销</th><th>交接时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in handovers" :key="item.交接编号">
            <td>{{ item.交接编号 }}</td>
            <td>{{ item.批次编号 }}</td>
            <td>{{ item.原接收方 }}</td>
            <td>{{ item.新接收方 }}</td>
            <td>{{ item.旧任务凭据 || '—' }}</td>
            <td>{{ item.撤销旧任务凭据 ? '是' : '否（原凭据仍有效）' }}</td>
            <td>{{ item.交接时间文本 }}</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="replay-card">
      <h3>执行重放并核对</h3>
      <div class="replay-grid">
        <label>
          <span>重放范围</span>
          <input v-model="replayRange" data-testid="replay-range" placeholder="起始批次-截止批次" />
        </label>
        <label>
          <span>覆盖方式</span>
          <select v-model="replayMode" data-testid="replay-mode">
            <option value="append">增量追加</option>
            <option value="overwrite">全量覆盖</option>
          </select>
        </label>
        <label>
          <span>凭据编号</span>
          <input v-model="replayCredential" data-testid="replay-credential" />
        </label>
        <button class="btn primary" data-testid="replay-create" type="button" @click="createTask">
          创建重放任务
        </button>
      </div>

      <p v-if="taskInfo" class="page-desc">
        当前任务 {{ taskInfo.replay_code }}（#{{ taskInfo.replay_id }}） · 接收方 {{ result?.target_code || '—' }}
      </p>

      <div v-if="result" class="replay-receipt" data-testid="replay-result">
        <strong>已写入 {{ result.records_replayed }} 条记录</strong>
        · 实际回取成员：{{ listText(result.authorized_members) }}
        · 未取成员：{{ deniedText }}
        <div class="replay-footer">
          回执 {{ result.receipt_code }} 保留当时资格：
          当时凭据状态 {{ result.receipt.当时凭据状态 }}，
          当时可回取范围 {{ listText(result.receipt.当时可回取范围) }}，
          原凭据有效范围 {{ listText(result.receipt.原凭据有效范围) }}。
          <span v-if="result.receipt.重新许可说明">{{ result.receipt.重新许可说明 }}。</span>
        </div>
      </div>

      <div v-if="approvals.length" data-testid="replay-approvals">
        <h4>待审批事项（需补许可）</h4>
        <table class="replay-table">
          <thead>
            <tr><th>审批编号</th><th>受影响成员</th><th>原接收方</th><th>现接收方</th><th>未取原因</th><th>需补事项</th></tr>
          </thead>
          <tbody>
            <tr v-for="approval in approvals" :key="approval.审批编号">
              <td>{{ approval.审批编号 }}</td>
              <td>{{ approval.受影响成员 }}</td>
              <td>{{ approval.原接收方 }}</td>
              <td>{{ approval.现接收方 }}</td>
              <td>{{ approval.未取原因 }}</td>
              <td>{{ approval.需补事项 }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="delivery" data-testid="replay-delivery">
        <h4>目标端实际数据核对</h4>
        <p class="page-desc">
          目标 {{ delivery.target_code }} 实际 {{ delivery.target_count }} 条
          · 回执 {{ delivery.receipt_code }}
          · 待补审批 {{ delivery.approval_count }} 笔
          · 回执原凭据有效范围 {{ listText(delivery.receipt_entitlement.原凭据有效范围) }}
        </p>
      </div>

      <p v-if="message" class="error-text" data-testid="replay-message">{{ message }}</p>

      <div class="replay-inline">
        <button class="btn primary" data-testid="replay-execute" type="button" :disabled="!taskInfo" @click="execute">
          执行重放
        </button>
        <button class="btn" data-testid="replay-verify" type="button" :disabled="!taskInfo" @click="verify">
          核对目标数据与审批
        </button>
      </div>

      <h4>按回执编号查当时资格</h4>
      <div class="replay-inline">
        <input v-model="receiptCode" data-testid="receipt-code" placeholder="RCPT-..." />
        <button class="btn" data-testid="receipt-load" type="button" @click="loadReceipt">
          读取回执
        </button>
      </div>
      <div v-if="receipt" class="replay-receipt" data-testid="receipt-detail">
        <strong>{{ receipt.回执编号 }}</strong> · {{ receipt.回执类型 }} · 接收方 {{ receipt.接收方 }}
        <div>当时凭据状态 {{ receipt.当时凭据状态 }} · 当时签发对象 {{ receipt.当时签发对象 }}</div>
        <div>当时可回取范围 {{ listText(receipt.当时可回取范围) }}</div>
        <div>本次回取成员 {{ listText(receipt.本次回取成员) }} · 写入 {{ receipt.写入记录数 }} 条</div>
        <div>原凭据有效范围 {{ listText(receipt.原凭据有效范围) }} · 需重新许可 {{ listText(receipt.需重新许可成员) }}</div>
        <div v-if="receipt.重新许可说明">{{ receipt.重新许可说明 }}</div>
        <div v-for="item in receipt.未取成员" :key="item.批次编号">
          未取 {{ item.批次编号 }}：{{ item.未取原因 }}
        </div>
        <div>生成于 {{ receipt.生成时间文本 }}</div>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createReplayTask,
  executeReplayTask,
  issueReplayCredential,
  listReplayApprovals,
  listReplayCredentials,
  listReplayHandovers,
  loadReplayDelivery,
  loadReplayReceipt,
  transferReplayBatch,
} from '@/api/backend-service'
import type {
  ReplayApproval,
  ReplayCredential,
  ReplayDelivery,
  ReplayExecutionResult,
  ReplayHandover,
  ReplayReceipt,
} from '@/data/types'

const archiveCode = ref('')
const receiver = ref('')
const memberInput = ref('')
const oneTime = ref(true)
const credentials = ref<ReplayCredential[]>([])
const handovers = ref<ReplayHandover[]>([])

const handoverBatch = ref('')
const handoverFrom = ref('')
const handoverTo = ref('')
const revokeOld = ref(false)

const replayRange = ref('')
const replayMode = ref('append')
const replayCredential = ref('')
const taskInfo = ref<{ replay_id: number; replay_code: string } | null>(null)
const result = ref<ReplayExecutionResult | null>(null)
const approvals = ref<ReplayApproval[]>([])
const delivery = ref<ReplayDelivery | null>(null)
const receiptCode = ref('')
const receipt = ref<ReplayReceipt | null>(null)
const message = ref('')

const deniedText = computed(() => {
  const denied = result.value?.denied_members ?? []
  if (!denied.length) return '无'
  return denied.map((item) => `${item.批次编号}（${item.未取原因}）`).join('；')
})

function listText(values: string[] | undefined): string {
  return values && values.length ? values.join('、') : '—'
}

function credentialTone(status: string): string {
  if (status === '已用') return 'ok'
  if (status === '已撤销' || status === '需重新许可') return 'danger'
  if (status === '部分有效') return 'warn'
  return ''
}

function members(): string[] {
  return memberInput.value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function report(error: unknown, fallback: string) {
  message.value = error instanceof Error ? error.message : fallback
}

async function refreshCredentials() {
  if (!archiveCode.value) {
    credentials.value = []
    return
  }
  credentials.value = await listReplayCredentials(archiveCode.value)
}

async function refreshHandovers() {
  if (!archiveCode.value) {
    handovers.value = []
    return
  }
  handovers.value = await listReplayHandovers(archiveCode.value)
}

async function refreshAll() {
  message.value = ''
  try {
    await Promise.all([refreshCredentials(), refreshHandovers()])
  } catch (error) {
    report(error, '重放凭据读取失败')
  }
}

async function issue() {
  message.value = ''
  try {
    const issued = await issueReplayCredential({
      archive_code: archiveCode.value,
      receiver: receiver.value,
      members: members().length ? members() : undefined,
      one_time: oneTime.value,
    })
    replayCredential.value = issued.credential_code
    await refreshAll()
  } catch (error) {
    report(error, '凭据签发失败')
  }
}

async function handover() {
  message.value = ''
  try {
    await transferReplayBatch({
      archive_code: archiveCode.value,
      batch_code: handoverBatch.value,
      from_receiver: handoverFrom.value,
      to_receiver: handoverTo.value,
      revoke_old_credential: revokeOld.value,
    })
    await refreshAll()
  } catch (error) {
    report(error, '批次转交失败')
  }
}

async function createTask() {
  message.value = ''
  try {
    taskInfo.value = await createReplayTask({
      archive_code: archiveCode.value,
      replay_range: replayRange.value,
      mode: replayMode.value,
      credential_code: replayCredential.value,
    })
    result.value = null
    approvals.value = []
    delivery.value = null
  } catch (error) {
    report(error, '重放任务创建失败')
  }
}

async function execute() {
  if (!taskInfo.value) return
  message.value = ''
  try {
    result.value = await executeReplayTask(taskInfo.value.replay_id)
    approvals.value = await listReplayApprovals(taskInfo.value.replay_code)
    await refreshAll()
  } catch (error) {
    report(error, '重放执行失败')
  }
}

async function loadReceipt() {
  if (!receiptCode.value) return
  message.value = ''
  try {
    receipt.value = await loadReplayReceipt(receiptCode.value)
  } catch (error) {
    report(error, '交付回执读取失败')
  }
}

async function verify() {
  if (!taskInfo.value) return
  message.value = ''
  try {
    delivery.value = await loadReplayDelivery(taskInfo.value.replay_id)
    approvals.value = delivery.value.pending_approvals
  } catch (error) {
    report(error, '目标数据核对失败')
  }
}

onMounted(refreshAll)
</script>
