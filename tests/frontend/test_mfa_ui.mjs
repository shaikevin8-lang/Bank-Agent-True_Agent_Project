/**
 * 前端「高风险操作确认卡」（MFA）的 DOM 集成测试。
 *
 * 用 jsdom 真实加载 web_test/index.html 并执行其中的内联脚本，再用打桩的 fetch
 * 模拟后端（严格按 docs/接口契约文档.md 的 confirm 接口响应格式），验证：
 * 卡片结构与无障碍标注、红色/黄色两条路径、6 格验证码输入（输入/退格/粘贴）、
 * 错误次数提示、重发倒计时、模拟短信面板只在演示角色下可见、
 * 聊天里发「确认 123456」被拦成引导且不泄漏验证码、刷新后从 /pending 恢复。
 *
 * 运行方式（Node 18+）：
 *     cd tests/frontend && node test_mfa_ui.mjs
 *
 * 依赖 jsdom 仅为测试用，不进入产品运行时；index.html 本身零外部依赖。
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const htmlPath = resolve(here, '../../Struct_of_Agent/web_test/index.html')

let failures = 0
let checks = 0

function check(name, condition, extra) {
  checks += 1
  if (condition) {
    console.log(`  ok   ${name}`)
  } else {
    failures += 1
    console.log(`  FAIL ${name}${extra === undefined ? '' : ' -> ' + JSON.stringify(extra)}`)
  }
}

function section(title) {
  console.log(`\n== ${title} ==`)
}

// ---------------------------------------------------------------- 响应桩

const NOW = '2026-01-01T10:00:00'
const CASE_NO = 'HITL-20260101-0001'
const REQUEST_ID = '3f1c2a90-0000-4000-8000-000000000001'

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

function ok(data, message = '') {
  return jsonResponse({ ok: true, message, error_code: null, data })
}

function fail(message, errorCode, status, data) {
  return jsonResponse({ ok: false, message, error_code: errorCode, data: data === undefined ? null : data }, status)
}

/** SSE 响应：页面发消息走流式接口，只用 jsonResponse 打桩会让聊天链路直接失败。 */
function sseResponse(events, status = 200) {
  const text =
    events.map((event) => 'data: ' + JSON.stringify(event) + '\n\n').join('') + 'data: [DONE]\n\n'
  return new Response(text, {
    status,
    headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
  })
}

const calls = []
const sessionState = { sessions: [], messages: {}, seq: 0 }

function createStubSession(title) {
  sessionState.seq += 1
  const session = {
    id: 'web-mfa-' + sessionState.seq,
    title: title || '新会话',
    created_at: NOW, updated_at: NOW,
    message_count: 0, preview: '', title_set_by_user: !!title,
  }
  sessionState.sessions.unshift(session)
  sessionState.messages[session.id] = []
  return session
}

function findStubSession(id) {
  return sessionState.sessions.filter((s) => s.id === id)[0] || null
}

/** SSE 里要推的「工具回执」：HITL 闸门拦截后由 result.tasks[] 带出来。 */
const chatState = {
  reply: '这次冻结需要你本人确认后才能执行，请在弹出的确认卡里完成。',
  receipt: null,
}

/** confirm 接口的运行态：决定红色/黄色与验证码校验结果。 */
const confirmState = {
  riskLevel: 'red',
  mfaRequired: true,
  mfaMode: 'ok',         // ok | invalid | locked
  pending: [],
  lastRequest: null,
}

async function stubFetch(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const body = options.body ? JSON.parse(options.body) : undefined
  calls.push({ url, method, body })

  if (url === '/api/auth/me') {
    return ok({
      authenticated: true,
      user: { username: 'tester', created_at: NOW, last_login_at: NOW },
      expires_at: '2026-12-31T00:00:00',
    })
  }
  if (url === '/health/live') {
    return jsonResponse({ status: 'ok', model_mode: 'mock', model: '', api_version: 'test' })
  }
  if (url === '/api/clock') {
    // 演示时钟未启用：页面按 NOT_READY_001 走「时钟不可用」，与本测试无关
    return fail('演示时钟未启用', 'NOT_READY_001', 409)
  }
  if (url.indexOf('/api/hitl/cases') === 0) {
    return ok({ items: [], total: 0 })
  }
  if (url === '/api/sessions' && method === 'GET') {
    return ok({ sessions: sessionState.sessions.slice() })
  }
  if (url === '/api/sessions' && method === 'POST') {
    return ok({ session: createStubSession(body && body.title) }, '已新建会话')
  }
  if (typeof url === 'string' && url.indexOf('/api/sessions/') === 0) {
    const id = decodeURIComponent(url.slice('/api/sessions/'.length))
    const session = findStubSession(id)
    if (!session) return fail('会话不存在或已被删除', 'SESSION_NOT_FOUND', 404)
    return ok({ session, messages: sessionState.messages[id] || [] })
  }
  if (url === '/api/chat/stream' && method === 'POST') {
    const session = findStubSession(body && body.session_id)
    if (session) {
      sessionState.messages[session.id].push({ role: 'user', content: body.message, created_at: NOW })
      sessionState.messages[session.id].push({ role: 'assistant', content: chatState.reply, created_at: NOW })
      session.message_count = sessionState.messages[session.id].length
    }
    const tasks = chatState.receipt ? [chatState.receipt] : []
    const result = {
      reply: chatState.reply, blocked: false, moderation: {}, tasks, agent: 'R4',
      trace: { session_id: body.session_id, steps: 1, tool_names: ['freeze_card'], model_mode: 'mock' },
    }
    return sseResponse([
      { type: 'plan', plan: [{ agent: 'R4', task: body.message }], router: 'llm' },
      { type: 'task_start', index: 0, total: 1, agent: 'R4', task: body.message },
      { type: 'token', index: 0, content: chatState.reply },
      { type: 'task_done', index: 0, total: 1, agent: 'R4', result },
      { type: 'done', result },
    ])
  }

  // ---- 确认凭据接口（契约见 docs/接口契约文档.md）
  if (url === '/api/v1/confirm/request' && method === 'POST') {
    confirmState.lastRequest = body
    return ok({
      request_id: REQUEST_ID,
      tool_name: body.tool_name,
      risk_level: confirmState.riskLevel,
      mfa_required: confirmState.mfaRequired,
      status: confirmState.mfaRequired ? 'pending' : 'issued',
      created_at: NOW,
      expires_at: '2026-01-01T10:03:00',
      args_redacted: body.args || {},
      args_summary: body.args || {},
      phone: '138****8888',
      phone_mask: confirmState.mfaRequired ? '138****8888' : '',
      ...(confirmState.mfaRequired ? {} : { grant_id: 'g-0000-0001' }),
    }, '已创建确认请求')
  }
  if (url === '/api/v1/confirm/mfa' && method === 'POST') {
    if (confirmState.mfaMode === 'invalid') {
      return fail('验证码错误', 'VALIDATION_001', 400, { reason: 'invalid_code', attempts_left: 2 })
    }
    if (confirmState.mfaMode === 'locked') {
      return fail('验证码错误次数过多', 'CONFLICT_001', 409, { reason: 'attempts_exceeded' })
    }
    return ok({ grant_id: 'g-0000-0001', request_id: REQUEST_ID, expires_at: '2026-01-01T10:15:00' }, '验证通过')
  }
  if (url.indexOf('/api/v1/confirm/sms/') === 0 && method === 'GET') {
    return ok({ request_id: REQUEST_ID, phone: '138****8888', code: '246810', expires_at: '2026-01-01T10:03:00' })
  }
  if (url.indexOf('/api/v1/confirm/pending') === 0 && method === 'GET') {
    return ok({ items: confirmState.pending, total: confirmState.pending.length })
  }

  throw new Error('未打桩的请求: ' + method + ' ' + url)
}

// ---------------------------------------------------------------- 装载页面

const html = readFileSync(htmlPath, 'utf8')

async function loadPage() {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://127.0.0.1:8000/',
    beforeParse(window) {
      window.fetch = stubFetch
    },
  })
  await new Promise((r) => setTimeout(r, 60))
  return dom
}

const dom = await loadPage()
const { window } = dom
const { document } = window

const settle = () => new Promise((r) => setTimeout(r, 40))
const $ = (id) => document.getElementById(id)
const click = (el) => el.dispatchEvent(new window.Event('click', { bubbles: true }))
const codeInputs = () => [...document.querySelectorAll('#mfaCode input[data-mfa-index]')]

/** 像用户一样逐格输入验证码（触发 input 事件，走页面的分发逻辑）。 */
function typeCode(text) {
  const inputs = codeInputs()
  inputs.forEach((input, index) => {
    input.value = text[index] || ''
    input.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
}

/** 在确认卡打开的前提下，把一次「工具被拦」的结果推给页面（等价于流式回执到达）。 */
async function pushBlockedReceipt(overrides = {}) {
  chatState.receipt = {
    tool: 'freeze_card',
    status: 'hitl_required',
    case_no: CASE_NO,
    reason_code: 'red_write',
    risk_level: 'red',
    sla_seconds: 180,
    expires_at: '2026-01-01T10:03:00',
    requires_user_confirmation: true,
    message: '该操作需要人工复核',
    confirmation: {
      tool_name: 'freeze_card',
      args: { card_no: '6222 0000 1111 2222', amount_fen: 128800 },
      args_fingerprint: 'fp-1',
      risk_level: 'red',
      mfa_required: true,
      requires_user_confirmation: true,
      case_no: CASE_NO,
    },
    ...overrides,
  }
  $('input').value = '冻结我的卡'
  click($('send'))
  await new Promise((r) => setTimeout(r, 120))
}

// ---------------------------------------------------------------- 静态结构

section('确认卡的结构与默认状态')
check('确认卡默认隐藏', $('mfaOverlay').hidden === true)
check('遮罩层是 mfa-overlay', $('mfaOverlay').className.indexOf('mfa-overlay') >= 0, $('mfaOverlay').className)
check('卡片有 role=dialog 且 aria-modal', $('mfaCard').getAttribute('role') === 'dialog' && $('mfaCard').getAttribute('aria-modal') === 'true')
check('标题通过 aria-labelledby 关联', $('mfaCard').getAttribute('aria-labelledby') === 'mfaTitle')
check('说明通过 aria-describedby 关联', $('mfaCard').getAttribute('aria-describedby') === 'mfaSub')
check('顶栏有「高风险确认」入口', !!$('mfaEntry'))
check('验证码共 6 格', codeInputs().length === 6, codeInputs().length)
check('每格限制 1 位', codeInputs().every((el) => el.getAttribute('maxlength') === '1'))
check('每格用数字键盘（inputmode=numeric）', codeInputs().every((el) => el.getAttribute('inputmode') === 'numeric'))
check('第一格支持 one-time-code 自动填充', codeInputs()[0].getAttribute('autocomplete') === 'one-time-code')
check('每格都有可读的 aria-label', codeInputs().every((el, i) => el.getAttribute('aria-label') === `验证码第 ${i + 1} 位`))
check('提示条是 aria-live 区域', $('mfaMsg').getAttribute('aria-live') === 'polite' && $('mfaMsg').getAttribute('role') === 'status')
check('模拟短信面板默认隐藏', $('mfaSms').hidden === true)

const styleText = document.querySelector('style').textContent.replace(/\/\*[\s\S]*?\*\//g, '')
check('.mfa-overlay 使用 display:flex（所以必须显式覆盖 [hidden]）',
  /\.mfa-overlay\s*\{[^}]*display:\s*flex/.test(styleText))
check('样式表显式把 .mfa-overlay[hidden] 置为 display:none',
  /\.mfa-overlay\[hidden\]\s*\{[^}]*display:\s*none/.test(styleText))
check('验证码块/不可逆警示/提示条都有 [hidden] 兜底',
  /\.mfa-code-block\[hidden\]/.test(styleText) &&
  /\.mfa-irreversible\[hidden\]/.test(styleText) &&
  /\.mfa-msg\[hidden\]/.test(styleText))

// ---------------------------------------------------------------- 红色路径

section('红色动作：聊天回执里的 confirmation 直接开卡（不用先手输参数）')
confirmState.riskLevel = 'red'
confirmState.mfaRequired = true
confirmState.mfaMode = 'ok'
await pushBlockedReceipt()

check('确认卡已打开', $('mfaOverlay').hidden === false)
check('同时渲染了「已转人工复核」卡片', !!document.querySelector('#chat') && /HITL-20260101-0001/.test($('chat').textContent))
const openCall = calls.filter((c) => c.url === '/api/v1/confirm/request').pop()
check('按 confirmation 建了确认请求', !!openCall)
check('请求体带 tool_name', openCall && openCall.body.tool_name === 'freeze_card', openCall && openCall.body)
check('请求体带脱敏参数（金额可读）', openCall && openCall.body.args && openCall.body.args.amount_fen === 128800, openCall && openCall.body.args)
check('请求体带 session_id', openCall && typeof openCall.body.session_id === 'string' && openCall.body.session_id.length > 0, openCall && openCall.body.session_id)
check('操作名翻译为「冻结卡片」', /冻结卡片/.test($('mfaTool').textContent), $('mfaTool').textContent)
check('展示了操作对象', /6222/.test($('mfaTarget').textContent), $('mfaTarget').textContent)
check('金额格式化为 1,288.00 元', $('mfaAmount').textContent === '1,288.00 元', $('mfaAmount').textContent)
check('红色风险徽标', $('mfaRisk').className.indexOf('yellow') < 0 && /高风险/.test($('mfaRiskText').textContent), $('mfaRiskText').textContent)
check('红色动作显示「不可撤销」警示', $('mfaIrreversible').hidden === false)
check('红色动作显示验证码输入区', $('mfaCodeBlock').hidden === false)
check('手机号以掩码展示', /138\*\*\*\*8888/.test($('mfaPhone').textContent), $('mfaPhone').textContent)
check('提示「已发送」且不显示任何明文验证码', /已发送/.test($('mfaMsg').textContent) && !/\d{6}/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
check('重发按钮初始禁用（倒计时中）', $('mfaResend').disabled === true)
check('倒计时数字已展示', /^\d{1,2}$/.test($('mfaResendLeft').textContent), $('mfaResendLeft').textContent)
check('普通用户看不到模拟短信面板', $('mfaSms').hidden === true)
check('普通用户不会请求 /sms 接口', !calls.some((c) => c.url.indexOf('/api/v1/confirm/sms/') === 0))

section('验证码输入：逐格输入 / 退格回退 / 粘贴分发')
const inputs = codeInputs()
inputs[0].value = '1'
inputs[0].dispatchEvent(new window.Event('input', { bubbles: true }))
check('输入一位后焦点前进到第 2 格', document.activeElement === inputs[1], document.activeElement && document.activeElement.getAttribute('data-mfa-index'))
inputs[1].dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }))
check('第 2 格为空时退格回到第 1 格', document.activeElement === inputs[0], document.activeElement && document.activeElement.getAttribute('data-mfa-index'))
check('退格清空了第 1 格', inputs[0].value === '', inputs[0].value)

const pasteEvent = new window.Event('paste', { bubbles: true, cancelable: true })
pasteEvent.clipboardData = { getData: () => '654321' }
inputs[0].dispatchEvent(pasteEvent)
check('粘贴 6 位码后自动分发到 6 格', inputs.map((el) => el.value).join('') === '654321', inputs.map((el) => el.value))
check('焦点落在最后一格', document.activeElement === inputs[5])

section('验证码错误：提示剩余次数并清空')
confirmState.mfaMode = 'invalid'
click($('mfaSubmit'))
await settle()
check('以 POST 提交 /api/v1/confirm/mfa', calls.some((c) => c.url === '/api/v1/confirm/mfa' && c.method === 'POST'))
const mfaCall = calls.filter((c) => c.url === '/api/v1/confirm/mfa').pop()
check('提交体带 request_id 与 6 位码', mfaCall.body.request_id === REQUEST_ID && /^\d{6}$/.test(mfaCall.body.code), mfaCall.body)
check('提示「还可尝试 2 次」', /还可尝试 2 次/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
check('错误后清空验证码', inputs.every((el) => el.value === ''))
check('错误后焦点回到第 1 格', document.activeElement === inputs[0])
check('错误后卡片仍打开', $('mfaOverlay').hidden === false)

section('验证码错误满 3 次：锁定输入但允许重发')
confirmState.mfaMode = 'locked'
typeCode('111111')
click($('mfaSubmit'))
await settle()
check('提示错误次数过多', /次数过多/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
check('6 格输入被禁用', inputs.every((el) => el.disabled === true))
check('重发按钮可用（唯一出路）', $('mfaResend').disabled === false)

section('验证通过：关闭卡片并提示重新发起操作')
confirmState.mfaMode = 'ok'
click($('mfaResend'))          // 重发会重开一条请求并重置成可输入状态
await settle()
check('重发后输入格恢复可用', codeInputs().every((el) => el.disabled === false))
typeCode('123456')
click($('mfaSubmit'))
await settle()
check('提交了正确的 6 位码', calls.filter((c) => c.url === '/api/v1/confirm/mfa').pop().body.code === '123456')
check('提示验证通过', /验证通过|已确认/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
await new Promise((r) => setTimeout(r, 1100))
check('成功提示后卡片自动关闭', $('mfaOverlay').hidden === true)
check('对话区追加了「请重新发起操作」的引导', /重新发起/.test($('chat').textContent))

section('取消与 Esc 关闭（清掉计时器）')
await pushBlockedReceipt()
check('卡片再次打开', $('mfaOverlay').hidden === false)
click($('mfaCancel'))
check('点「取消」关闭卡片', $('mfaOverlay').hidden === true)
await pushBlockedReceipt({ case_no: 'HITL-20260101-0002' })
document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
check('Esc 关闭卡片', $('mfaOverlay').hidden === true)

section('黄色动作：点确认即生效，不要验证码')
confirmState.riskLevel = 'yellow'
confirmState.mfaRequired = false
await pushBlockedReceipt({ case_no: 'HITL-20260101-0003' })
check('黄色卡片已打开', $('mfaOverlay').hidden === false)
check('黄色风险徽标为「中风险」', /中风险/.test($('mfaRiskText').textContent), $('mfaRiskText').textContent)
check('黄色动作隐藏验证码输入区', $('mfaCodeBlock').hidden === true)
check('黄色动作隐藏重发按钮', $('mfaResend').hidden === true)
check('提示已确认', /已确认/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
await new Promise((r) => setTimeout(r, 1100))
check('黄色动作无需输码即关闭', $('mfaOverlay').hidden === true)
confirmState.riskLevel = 'red'
confirmState.mfaRequired = true

// ---------------------------------------------------------------- 聊天拦截

section('聊天里发「确认 123456」：不当确认、不回显、不送模型')
const chatCallsBefore = calls.filter((c) => c.url === '/api/chat/stream').length
confirmState.pending = [{
  request_id: REQUEST_ID, tool_name: 'freeze_card', risk_level: 'red', mfa_required: true,
  status: 'pending', expires_at: '2026-01-01T10:03:00', phone: '138****8888',
  args_redacted: { card_no: '6222 0000 1111 2222' },
}]
$('input').value = '确认 123456'
click($('send'))
await new Promise((r) => setTimeout(r, 150))
const chatCallsAfter = calls.filter((c) => c.url === '/api/chat/stream').length
check('没有把整句话发给模型', chatCallsAfter === chatCallsBefore, [chatCallsBefore, chatCallsAfter])
check('用户气泡只显示「（验证码已隐藏）」', /（验证码已隐藏）/.test($('chat').textContent))
check('给出的引导说明验证码不能走聊天', /不能通过聊天|不会.*当作确认|卡片里输入/.test($('chat').textContent))
check('引导后从 /pending 恢复确认卡', calls.some((c) => c.url.indexOf('/api/v1/confirm/pending') === 0))
check('确认卡随之打开', $('mfaOverlay').hidden === false)
check('恢复的卡片带出了操作对象', /6222/.test($('mfaTarget').textContent), $('mfaTarget').textContent)
check('聊天记录里没有泄漏 6 位验证码', !/确认 123456/.test($('chat').textContent))
click($('mfaCancel'))

section('顶栏「高风险确认」入口：没有待确认项时给出提示')
confirmState.pending = []
const pendingBefore = calls.filter((c) => c.url.indexOf('/api/v1/confirm/pending') === 0).length
click($('mfaEntry'))
await new Promise((r) => setTimeout(r, 120))
check('点击入口会查 /pending', calls.filter((c) => c.url.indexOf('/api/v1/confirm/pending') === 0).length > pendingBefore)
check('无待确认项时不开卡', $('mfaOverlay').hidden === true)
check('并给出「没有待确认」的提示', /没有待确认/.test($('chat').textContent), $('chat').textContent.slice(-120))

// ---------------------------------------------------------------- 演示角色

section('模拟短信面板：仅演示者（复核员/运维）可见')
confirmState.pending = [{
  request_id: REQUEST_ID, tool_name: 'freeze_card', risk_level: 'red', mfa_required: true,
  status: 'pending', expires_at: '2026-01-01T10:03:00', phone_mask: '138****8888',
  args_redacted: { card_no: '6222 0000 1111 2222' },
}]
const roleSel = $('roleSelect')
roleSel.value = 'risk_reviewer'
roleSel.dispatchEvent(new window.Event('change', { bubbles: true }))
await settle()
click($('mfaEntry'))
await new Promise((r) => setTimeout(r, 150))
check('复核员能看到模拟短信面板', $('mfaSms').hidden === false)
check('复核员会请求 /sms 接口', calls.some((c) => c.url.indexOf('/api/v1/confirm/sms/') === 0))
check('面板里展示明文验证码（仅演示）', /246810/.test($('mfaSmsBody').textContent), $('mfaSmsBody').textContent)
click($('mfaCancel'))
roleSel.value = 'user'
roleSel.dispatchEvent(new window.Event('change', { bubbles: true }))
await settle()

// ---------------------------------------------------------------- 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
// jsdom 里还挂着页面的计时器（时钟/倒计时），不显式退出会把进程一直吊着
process.exit(0)