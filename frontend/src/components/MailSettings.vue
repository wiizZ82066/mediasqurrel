<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'

const settings = ref(null), example = ref(null), busy = ref(''), error = ref(''), notice = ref('')
const fields = reactive({ sender: '', recipient: '', code: '', enabled: false })
const confirming = ref(''), expanded = ref(false)
const labels = { queued: '等待发送', preparing: '正在准备邮件', sending: '正在发送', sent: 'QQ 邮箱已接收',
  failed: '发送失败', uncertain: '需要核对收件箱', cancelled: '已取消' }
let timer, alive = true
const changed = computed(() => settings.value && (fields.sender.trim() !== settings.value.sender ||
  fields.recipient.trim() !== settings.value.recipient || fields.enabled !== settings.value.enabled || !!fields.code))
const needsCode = computed(() => !settings.value?.has_code || fields.sender.trim().toLowerCase() !== settings.value.sender)

function apply(value) {
  settings.value = value
  clearTimeout(timer)
  if (alive && value.active && !document.hidden) timer = setTimeout(refresh, value.recent.some(r => r.state === 'sending') ? 2000 : 5000)
}
async function refresh() {
  try { const value = await api.mailSettings(); if (alive) { apply(value); if (!busy.value) error.value = '' } }
  catch { if (alive) { error.value = '暂时无法读取投递状态。'; timer = setTimeout(refresh, 10000) } }
}
function visible() { if (!document.hidden) refresh(); else clearTimeout(timer) }
async function action(key, fn) {
  if (busy.value) return
  busy.value = key; error.value = ''; notice.value = ''
  try { await fn() } catch (e) { error.value = e.message }
  finally { busy.value = '' }
}
async function load() {
  await action('load', async () => {
    const value = await api.mailSettings()
    if (!alive) return
    apply(value)
    Object.assign(fields, { sender: value.sender, recipient: value.recipient, enabled: value.enabled })
    expanded.value = !value.has_code
  })
}
async function save() {
  await action('save', async () => {
    const value = await api.saveMail({ ...fields, code: fields.code.trim() })
    fields.code = ''; fields.sender = value.sender; fields.recipient = value.recipient
    apply(value); notice.value = '邮箱设置已保存。'
  })
}
async function preview() {
  await action('preview', async () => { example.value = await api.mailSample() })
}
async function test() {
  await action('test', async () => {
    const result = await api.sendMailSample(example.value.task_id)
    const value = await api.mailSettings()
    apply(value)
    const row = value.recent.find(r => r.id === result.id)
    notice.value = row?.state === 'sent' ? '这条测试作品已经投递，请查看收件箱。' : '请在投递记录中查看这条测试作品的状态。'
  })
}
async function retry(row) {
  if (row.state === 'uncertain' && confirming.value !== row.id) { confirming.value = row.id; return }
  await action('retry', async () => {
    await api.retryMail(row.id, confirming.value === row.id)
    confirming.value = ''; apply(await api.mailSettings())
  })
}
function date(seconds) { return new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
onMounted(() => { load(); document.addEventListener('visibilitychange', visible) })
onBeforeUnmount(() => { alive = false; clearTimeout(timer); fields.code = ''; document.removeEventListener('visibilitychange', visible) })
</script>

<template>
  <section class="mail-section" aria-labelledby="mail-heading">
    <h2 id="mail-heading">邮件通知</h2>
    <div class="mail-sheet">
      <button class="mail-toggle" type="button" :aria-expanded="expanded" aria-controls="mail-preferences" @click="expanded = !expanded">
        <span><strong>QQ 邮箱</strong><small>{{ settings?.enabled ? '作品附件和录播完成提醒将发到 ' + settings.recipient : '接收作品附件和录播完成提醒' }}</small></span>
        <span class="toggle-label">{{ expanded ? '收起' : '设置' }}</span>
      </button>
      <div v-if="expanded && settings" id="mail-preferences" class="mail-body">
        <p v-if="!settings.supported" class="hint">保存授权码需要 Windows 用户账户。</p>
        <form @submit.prevent="save">
          <div class="addresses">
            <label>发件邮箱<input v-model="fields.sender" type="email" required maxlength="76" autocomplete="off" spellcheck="false" placeholder="用于发送作品的 QQ 邮箱"></label>
            <label>收件邮箱<input v-model="fields.recipient" type="email" required maxlength="76" autocomplete="off" spellcheck="false" placeholder="微信 QQ 邮箱提醒绑定的邮箱"></label>
          </div>
          <label>发件邮箱授权码<input v-model="fields.code" type="password" :required="needsCode" maxlength="64" autocomplete="new-password"
            :placeholder="needsCode ? '在 QQ 邮箱的账号与安全设置中生成' : '已安全保存，留空可继续使用'"></label>
          <p class="hint">在发件邮箱开启 SMTP 后生成授权码。<a href="https://mail.qq.com/" target="_blank" rel="noopener noreferrer">打开 QQ 邮箱</a></p>
          <label class="check-line"><input v-model="fields.enabled" type="checkbox">下载或录播完成后自动发邮件</label>
          <p class="hint">下载作品随邮件附上，大作品分封发送。录播正常下播或手动停止并入库后，只发送文字摘要。</p>
          <button class="btn btn-primary btn-sm" type="submit" :disabled="!!busy || !settings.supported || !changed">{{ busy === 'save' ? '正在保存…' : '保存邮箱设置' }}</button>
        </form>
        <div class="test-area">
          <h3>测试一条已下载的作品</h3>
          <p class="hint">微信启用「QQ 邮箱提醒」并绑定收件邮箱后，可从提醒进入同一封邮件查看附件。</p>
          <button v-if="!example" type="button" class="btn btn-ghost btn-sm" :disabled="!!busy" @click="preview">{{ busy === 'preview' ? '正在查找…' : '预览测试作品' }}</button>
          <div v-else class="example">
            <div><strong>{{ example.author }} · {{ example.title }}</strong><small>{{ example.platform }} · {{ example.count }} 个附件 · {{ (example.bytes / 1048576).toFixed(1) }} MB</small></div>
            <button type="button" class="btn btn-ghost btn-sm" :disabled="!!busy || !settings.has_code || changed" @click="test">{{ busy === 'test' ? '正在提交…' : '发送测试邮件' }}</button>
          </div>
        </div>
        <div v-if="settings.recent.length" class="history">
          <h3>最近投递</h3>
          <div v-for="row in settings.recent" :key="row.id" class="receipt">
            <div><strong>{{ row.recording ? '录播完成' : row.sample ? '测试作品' : '下载作品' }} · {{ labels[row.state] }}</strong>
              <p v-if="row.caption">{{ row.caption }}</p>
              <small>{{ date(row.updated) }}<template v-if="row.total"> · {{ row.done }}/{{ row.total }} 封</template></small>
              <p v-if="row.reason">{{ row.reason }}</p></div>
            <button v-if="['failed','uncertain'].includes(row.state)" type="button" class="btn btn-ghost btn-sm" :disabled="!!busy" @click="retry(row)">{{ confirming === row.id ? '确认未收到，重试' : '重试' }}</button>
          </div>
        </div>
      </div>
      <p v-if="notice" class="feedback" role="status">{{ notice }}</p>
      <p v-if="error" class="feedback error" role="alert">{{ error }} <button v-if="!settings" type="button" class="btn btn-ghost btn-sm" @click="load">重试</button></p>
    </div>
  </section>
</template>

<style scoped>
.mail-section{margin:0 0 30px}.mail-section h2{font-size:16px;line-height:1.5;margin:0 0 12px;font-weight:650}
.mail-sheet{border:1px solid var(--border);border-radius:14px;background:var(--surface);box-shadow:var(--shadow-card)}
.mail-toggle,.example,.receipt{display:flex;align-items:center;justify-content:space-between;gap:16px}
.mail-toggle{width:100%;border:0;border-radius:14px;background:transparent;color:inherit;text-align:left;padding:17px 20px;cursor:pointer;font:inherit}
.mail-toggle:hover{background:var(--surface-hover,#f5f7f9)}.toggle-label,.hint a{color:var(--accent,#375e96)}.toggle-label{font-size:13px;flex-shrink:0}
.mail-body{border-top:1px solid var(--border);padding:18px 20px}.mail-section strong,.mail-section h3{font-size:14px;font-weight:650}
.mail-section small,.hint,.receipt p,.feedback{display:block;color:var(--text-secondary,#536073);font-size:13px;line-height:1.6}
.mail-section small{margin-top:3px}.hint,.receipt p{margin:4px 0 12px}.mail-body form>label,.addresses label{display:grid;gap:6px;font-size:13px;font-weight:600}
.addresses{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:12px}
.mail-body input:is([type=email],[type=password]){width:100%;min-width:0;box-sizing:border-box;padding:9px 10px;color:inherit;border:1px solid var(--border);border-radius:8px;background:var(--surface);font:inherit;font-weight:400}
.mail-body form>.check-line{display:flex;align-items:center;gap:9px;margin:18px 0 2px;font-size:14px}.check-line input{width:16px;height:16px;accent-color:var(--accent)}
.test-area,.history{border-top:1px solid var(--border);margin-top:18px;padding-top:16px}.mail-section h3{margin:0}
.example>div,.receipt>div{min-width:0;overflow-wrap:anywhere}.example button,.receipt button{flex-shrink:0}
.receipt{padding:10px 0;border-bottom:1px solid var(--border)}.receipt:last-child{border:0;padding-bottom:0}.receipt p{margin:4px 0}
.feedback{margin:0;padding:0 20px 14px}.error{color:var(--danger,#a83932)}.hint a{text-underline-offset:3px}
.mail-section :focus-visible{outline:2px solid var(--accent,#375e96);outline-offset:3px}
@media(max-width:650px){.addresses{grid-template-columns:1fr;gap:12px}.mail-body,.mail-toggle{padding:15px}.example{align-items:flex-start;flex-direction:column}}
</style>
