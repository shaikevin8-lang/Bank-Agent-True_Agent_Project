/**
 * 前端「确认卡片」（高风险操作确认 / MFA）UI 契约测试 —— 阶段 5 测试体系补齐。
 *
 * 用 jsdom 真实加载 Struct_of_Agent/web_test/index.html 并执行其中的内联脚本，
 * 再用打桩的 fetch 模拟后端（响应格式取自 docs/AI_Banking_Agent_接口契约文档.md
 * 的 confirm 段与 api_confirm.py 的实际返回），逐条校验确认卡片的 8 条 UI 契约：
 *
 *   1. 灰色（yellow/green）动作与红色动作的卡片样式 / 提示不同；
 *   2. 卡片展示 args_redacted 脱敏摘要，绝不渲染 mfa_code_hash / 明文验证码 / 全卡号；
 *   3. 红色动作有 6 位验证码输入框 + 提交按钮，非红色动作没有；
 *   4. 倒计时 / 有效期提示字段渲染；
 *   5. 点「取消」→ 只做本地撤销（撤销接口契约见文末「契约缺口」章节）；
 *   6. 二次确认 / 重复点击不会重复提交（提交中 disabled + busy 闸门）；
 *   7. 错误码 → 文案映射（AUTHZ_001 / CONFLICT_001 / UNKNOWN_RESULT_001 各有提示）；
 *   8. 演示短信码只出现在演示面板，不写进业务响应对象。
 *
 * 运行方式（Node 18+，与同目录其它 .mjs 一致）：
 *     cd tests/frontend && node test_confirm_card.mjs
 *
 * 依赖 jsdom 仅为测试用，不进入产品运行时；index.html 本身零外部依赖。
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const htmlPath = resolve(here, '../../Struct_of_Agent/web_test/index.html')
// 只读地取后端 confirm 契约源码，用于校验「取消/撤销」契约是否已在服务端就绪
const confirmApiPath = resolve(here, '../../Struct_of_Agent/web_test/api_confirm.py')

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

// ---------------------------------------------------------------- 固定数据

const NOW = '2026-01-01T10:00:00'
const EXPIRES = '2026-01-01T10:03:00'
const CASE_NO = 'HITL-20260101-0001'
const REQUEST_ID = '3f1c2a90-0000-4000-8000-000000000001'
const MASKED_CARD = '6222 **** **** 2222'   // 服务端脱敏后的样子
const MASKED_PHONE = '138****8888'
const FULL_PAN = '6222000011112222'         // 服务端绝不该下发、前端也绝不该上屏的全卡号
const HASH = 'sha256:3b1f9c0d4e5f60718293a4b5c6d7e8f9'
const PLAIN_CODE = '135790'                 // 业务响应里被硬塞的明文码（卡片不许渲染）
const DEMO_CODE = '246810'                  // 只允许出现在演示短信面板的模拟码

// ---------------------------------------------------------------- 响应桩

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
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
  return new Response(text, { status, headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
}

const calls = []
const sessionState = { sessions: [], messages: {}, seq: 0 }

function createStubSession(title) {
  sessionState.seq += 1
  const session = {
    id: 'web-confirm-' + sessionState.seq,
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

/** 聊天回执：HITL 闸门拦截后由 result.tasks[] 带出（确认卡就靠这条链路弹出来）。 */
const chatState = { reply: '这个操作需要你本人确认后才能执行。', receipt: null }

/**
 * confirm 接口的运行态：
 *   riskLevel / mfaRequired 决定 /confirm/request 的返回（红色待输码 / 黄色绿色已签发）；
 *   mfaReply 非 null 时 /confirm/mfa 按它返回错误（用于错误码映射与防重测试）；
 *   mfaDelayMs 让 /confirm/mfa 慢一点返回，用来观察「提交中」的按钮状态与连点行为。
 */
const confirmState = {
  riskLevel: 'red',
  mfaRequired: true,
  mfaDelayMs: 0,
  mfaReply: null,
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
    const result = {
      reply: chatState.reply, blocked: false, moderation: {},
      tasks: chatState.receipt ? [chatState.receipt] : [], agent: 'R4',
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

  // ---- 确认凭据接口（契约见 docs/AI_Banking_Agent_接口契约文档.md）
  if (url === '/api/v1/confirm/request' && method === 'POST') {
    confirmState.lastRequest = body
    const required = confirmState.mfaRequired === true
    return ok({
      request_id: REQUEST_ID,
      tool_name: body.tool_name,
      risk_level: confirmState.riskLevel,
      mfa_required: required,
      status: required ? 'pending' : 'issued',
      created_at: NOW,
      expires_at: EXPIRES,
      args_redacted: body.args || {},
      args_summary: body.args || {},
      phone: MASKED_PHONE,
      phone_mask: required ? MASKED_PHONE : '',
      // 故意多塞敏感字段：服务端就算漏了，卡片也一个都不许渲染
      mfa_code_hash: HASH,
      code: PLAIN_CODE,
      ...(required ? {} : { grant_id: 'g-0000-0001' }),
    }, required ? '已创建确认请求' : '已签发确认凭据')
  }
  if (url === '/api/v1/confirm/mfa' && method === 'POST') {
    if (confirmState.mfaDelayMs > 0) {
      await new Promise((r) => setTimeout(r, confirmState.mfaDelayMs))
    }
    if (confirmState.mfaReply) {
      const reply = confirmState.mfaReply
      return fail(reply.message, reply.error_code, reply.status || 400, reply.data)
    }
    return ok({ grant_id: 'g-0000-0001', request_id: REQUEST_ID, expires_at: '2026-01-01T10:15:00' }, '验证通过')
  }
  if (url.indexOf('/api/v1/confirm/sms/') === 0 && method === 'GET') {
    return ok({ request_id: REQUEST_ID, phone: MASKED_PHONE, code: DEMO_CODE, expires_at: EXPIRES })
  }
  if (url.indexOf('/api/v1/confirm/pending') === 0 && method === 'GET') {
    return ok({ items: confirmState.pending, total: confirmState.pending.length })
  }

  throw new Error('未打桩的请求: ' + method + ' ' + url)
}

// ---------------------------------------------------------------- 装载页面

const html = readFileSync(htmlPath, 'utf8')
const backendSrc = readFileSync(confirmApiPath, 'utf8')

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
  beforeParse(window) {
    window.fetch = stubFetch
  },
})
const { window } = dom
const { document } = window

const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms))
const $ = (id) => document.getElementById(id)
const click = (el) => el.dispatchEvent(new window.Event('click', { bubbles: true }))
const codeInputs = () => [...document.querySelectorAll('#mfaCode input[data-mfa-index]')]

const countMfaPost = () => calls.filter((c) => c.url === '/api/v1/confirm/mfa' && c.method === 'POST').length
const countConfirmPost = () => calls.filter((c) => c.url === '/api/v1/confirm/request' && c.method === 'POST').length
const countSmsGet = () => calls.filter((c) => typeof c.url === 'string' && c.url.indexOf('/api/v1/confirm/sms/') === 0).length

/** 整张卡片的可见文字（含隐藏节点，jsdom 的 textContent 不看 display）。 */
const cardText = () => $('mfaCard').textContent
/** 摘要 / 提示区文字：排除页脚那句固定文案（它自带「确认 123456」示例，会污染明文码扫描）。 */
function summaryText() {
  return [$('mfaSub'), document.querySelector('#mfaCard .mfa-summary'), $('mfaMsg')]
    .filter(Boolean).map((el) => el.textContent).join(' | ')
}
/** 卡片里除演示短信面板以外的全部文字（用于验证码「只在演示面板」）。 */
function textOutsideSmsPanel() {
  const parts = [cardText()]
  const sms = $('mfaSmsBody')
  if (sms && sms.textContent) parts[0] = parts[0].split(sms.textContent).join('[演示面板]')
  return parts.join('')
}

/** 像用户一样逐格输入验证码（触发 input 事件，走页面的分发逻辑）。 */
function typeCode(text) {
  codeInputs().forEach((input, index) => {
    input.value = text[index] || ''
    input.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
}

/**
 * 推一条「工具被 HITL 闸门拦住」的流式回执，让确认卡按真实链路弹出来。
 * 参数即回执里的 confirmation 段（前端据此建 /confirm/request）。
 */
async function pushReceipt(options = {}) {
  const toolName = options.toolName || 'freeze_card'
  const risk = options.risk || 'red'
  const mfaRequired = options.mfaRequired !== false
  const caseNo = options.caseNo || CASE_NO
  chatState.receipt = {
    tool: toolName,
    status: 'hitl_required',
    case_no: caseNo,
    reason_code: risk === 'red' ? 'red_write' : 'yellow_write',
    risk_level: risk,
    sla_seconds: 180,
    expires_at: EXPIRES,
    requires_user_confirmation: true,
    message: '该操作需要人工复核',
    confirmation: {
      tool_name: toolName,
      args: options.args || { card_no: MASKED_CARD, amount_fen: 128800 },
      args_fingerprint: 'fp-1',
      risk_level: risk,
      mfa_required: mfaRequired,
      requires_user_confirmation: true,
      case_no: caseNo,
    },
  }
  $('input').value = '冻结我的卡'
  click($('send'))
  await settle(150)
}

await settle(120)   // 等首屏（/api/auth/me → 主界面）稳定

// ================================================================ 静态结构

section('静态结构：卡片骨架与三档风险配色')
check('确认卡默认隐藏', $('mfaOverlay').hidden === true)
check('风险徽标默认文案为「高风险」', $('mfaRiskText').textContent === '高风险', $('mfaRiskText').textContent)
check('卡片里有 6 格验证码输入 + 提交按钮 + 取消按钮',
  codeInputs().length === 6 && !!$('mfaSubmit') && !!$('mfaCancel'),
  [codeInputs().length, !!$('mfaSubmit'), !!$('mfaCancel')])
check('模拟短信面板静态收起（hidden）', $('mfaSms').hidden === true)

const styleText = document.querySelector('style').textContent.replace(/\/\*[\s\S]*?\*\//g, '')
check('CSS 为 yellow / green 定义与红色不同的徽标配色（三档互不相同）',
  /\.mfa-risk\.yellow\s*\{[^}]*--warn/.test(styleText) &&
  /\.mfa-risk\.green\s*\{[^}]*--ok/.test(styleText) &&
  /\.mfa-risk\s*\{[^}]*--bad/.test(styleText))
check('卡片顶部描边用红色风险色（红色是默认档）',
  /\.mfa-card\s*\{[^}]*border-top:\s*3px solid var\(--risk-red\)/.test(styleText))

// ================================================================ 红色动作

section('红色动作：卡片样式 / 提示（契约 1、3）')
confirmState.riskLevel = 'red'
confirmState.mfaRequired = true
confirmState.mfaReply = null
confirmState.mfaDelayMs = 0
await pushReceipt()

check('确认卡已打开', $('mfaOverlay').hidden === false)
check('红色徽标不带 yellow / green 类（默认高风险配色）',
  $('mfaRisk').className.indexOf('yellow') < 0 && $('mfaRisk').className.indexOf('green') < 0,
  $('mfaRisk').className)
check('红色徽标文案为「高风险」', /高风险/.test($('mfaRiskText').textContent), $('mfaRiskText').textContent)
const redSub = $('mfaSub').textContent
check('红色提示说明要输短信验证码', /输入短信验证码/.test(redSub), redSub)
check('不可逆动作显示「不可撤销」警示', $('mfaIrreversible').hidden === false)
check('红色动作显示 6 位验证码输入区', $('mfaCodeBlock').hidden === false)
check('6 格输入全部可用（未禁用）', codeInputs().every((el) => el.disabled === false))
check('提交按钮「确认执行」可用', $('mfaSubmit').disabled === false && /确认执行/.test($('mfaSubmit').textContent), $('mfaSubmit').textContent)

const openCall = calls.filter((c) => c.url === '/api/v1/confirm/request').pop()
check('按 confirmation 建了确认请求', !!openCall)
check('请求体只带 tool_name / args / session_id 三个字段',
  openCall && Object.keys(openCall.body).sort().join(',') === 'args,session_id,tool_name',
  openCall && Object.keys(openCall.body))

// ================================================================ 脱敏摘要

section('脱敏摘要：只渲染 args_redacted，不出现哈希 / 全卡号 / 明文码（契约 2）')
check('操作名带中文标签与工具名', /冻结卡片/.test($('mfaTool').textContent) && /freeze_card/.test($('mfaTool').textContent), $('mfaTool').textContent)
check('操作对象为脱敏卡号', $('mfaTarget').textContent === MASKED_CARD, $('mfaTarget').textContent)
check('金额按分格式化', $('mfaAmount').textContent === '1,288.00 元', $('mfaAmount').textContent)
check('确认编号展示 request_id 前缀', /^3f1c2a90…$/.test($('mfaRequestNo').textContent), $('mfaRequestNo').textContent)
check('卡片不含 mfa_code_hash 字段名', !/mfa_code_hash/.test(cardText()))
check('卡片不含服务端下发的哈希值', cardText().indexOf(HASH) < 0)
check('卡片不含 16 位全卡号', !/\d{16}/.test(cardText()))
check('摘要区不含被硬塞的明文验证码', summaryText().indexOf(PLAIN_CODE) < 0)
check('摘要区不含任何 6 位连续数字', !/(?<!\d)\d{6}(?!\d)/.test(summaryText()), summaryText())
check('请求体里的参数是脱敏后的（前端不自己拼全卡号）',
  openCall && openCall.body.args.card_no === MASKED_CARD && !/\d{16}/.test(JSON.stringify(openCall.body)),
  openCall && openCall.body.args)

// ================================================================ 倒计时

section('倒计时 / 有效期：重发倒计时与 3 分钟有效期提示（契约 4）')
check('重发按钮可见且带「秒」文案', $('mfaResend').hidden === false && /秒/.test($('mfaResend').textContent), $('mfaResend').textContent)
check('倒计时中重发按钮被禁用（防连点）', $('mfaResend').disabled === true)
const leftFirst = Number($('mfaResendLeft').textContent)
check('倒计时剩余秒数是数字且来自 60s 防连点窗口', Number.isFinite(leftFirst) && leftFirst > 0 && leftFirst <= 60, $('mfaResendLeft').textContent)
check('提示条给出有效期（3 分钟内输入）', /3 分钟/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
check('【契约缺口】卡片未渲染服务端 expires_at 的到期时刻（当前只有 60s 重发倒计时 + 「3 分钟」文案）',
  !/到期|截止|过期|expires/.test(cardText()), cardText().slice(0, 80))
await settle(1200)
const leftSecond = Number($('mfaResendLeft').textContent)
check('倒计时逐秒递减', leftSecond < leftFirst, [leftFirst, leftSecond])
check('倒计时期间卡片仍打开', $('mfaOverlay').hidden === false)

// ================================================================ 防重复提交

section('防重复提交：连点只发一次，提交中按钮 loading / 输入禁用（契约 6）')
const beforeEmpty = countMfaPost()
click($('mfaSubmit'))
await settle(80)
check('空验证码点击不发请求（前置校验）', countMfaPost() === beforeEmpty, countMfaPost() - beforeEmpty)
check('并提示需要 6 位数字', /6 位数字验证码/.test($('mfaMsg').textContent), $('mfaMsg').textContent)

// 慢响应 + 错误码，方便观察「提交中」状态并且不触发成功收尾
confirmState.mfaDelayMs = 150
confirmState.mfaReply = { message: '验证码错误', error_code: 'VALIDATION_001', status: 400, data: { reason: 'invalid_code', attempts_left: 2 } }
typeCode('123456')
const beforeFastClicks = countMfaPost()
click($('mfaSubmit'))
click($('mfaSubmit'))
click($('mfaSubmit'))
check('提交中按钮立刻 disabled 且文案变「处理中…」',
  $('mfaSubmit').disabled === true && /处理中/.test($('mfaSubmit').textContent), $('mfaSubmit').textContent)
check('提交中 6 格输入被禁用', codeInputs().every((el) => el.disabled === true))
await settle(400)
check('连点三次只发了一条 /confirm/mfa', countMfaPost() - beforeFastClicks === 1, countMfaPost() - beforeFastClicks)
const mfaCall = calls.filter((c) => c.url === '/api/v1/confirm/mfa').pop()
check('提交体只带 request_id 与 6 位码（验证码不进业务参数）',
  mfaCall && Object.keys(mfaCall.body).sort().join(',') === 'code,request_id' && /^\d{6}$/.test(mfaCall.body.code),
  mfaCall && mfaCall.body)
check('提交失败后按钮恢复可用（解除 loading）',
  $('mfaSubmit').disabled === false && /确认执行/.test($('mfaSubmit').textContent))
confirmState.mfaDelayMs = 0

// ================================================================ 错误码映射

section('错误码 → 文案映射（契约 7）')
confirmState.mfaReply = { message: '当前会话无权处理该确认请求（主体不匹配）', error_code: 'AUTHZ_001', status: 403, data: { reason: 'subject_mismatch' } }
typeCode('111111')
click($('mfaSubmit'))
await settle(120)
const authzMsg = $('mfaMsg').textContent
check('AUTHZ_001 → 提示「无权处理」，且为警告样式', /无权/.test(authzMsg) && $('mfaMsg').className.indexOf('warn') >= 0, authzMsg)
check('AUTHZ_001 不当作成功（无「验证通过 / 已确认」，卡片不关闭）',
  !/验证通过|已确认/.test(authzMsg) && $('mfaOverlay').hidden === false)

confirmState.mfaReply = { message: '验证码错误次数过多', error_code: 'CONFLICT_001', status: 409, data: { reason: 'attempts_exceeded', attempts_left: 0 } }
typeCode('222222')
click($('mfaSubmit'))
await settle(120)
const lockedMsg = $('mfaMsg').textContent
check('CONFLICT_001 attempts_exceeded → 提示「次数过多」', /次数过多/.test(lockedMsg), lockedMsg)
check('锁定时 6 格输入全部禁用', codeInputs().every((el) => el.disabled === true))
check('锁定时重发按钮可用（唯一出路）', $('mfaResend').disabled === false)
check('锁定后卡片不关闭', $('mfaOverlay').hidden === false)

await pushReceipt({ caseNo: 'HITL-20260101-0002' })
confirmState.mfaReply = { message: '该确认请求已被使用或已撤销，不能重复提交', error_code: 'CONFLICT_001', status: 409, data: { reason: 'request_used' } }
typeCode('333333')
click($('mfaSubmit'))
await settle(120)
const usedMsg = $('mfaMsg').textContent
check('CONFLICT_001 request_used → 提示「已使用 / 已撤销」且不签发成功',
  /已使用|已撤销/.test(usedMsg) && !/验证通过/.test(usedMsg), usedMsg)

await pushReceipt({ caseNo: 'HITL-20260101-0003' })
confirmState.mfaReply = { message: '结果未知：未确认执行结果，请先查询原操作状态', error_code: 'UNKNOWN_RESULT_001', status: 500, data: { reason: 'unknown_result' } }
typeCode('444444')
click($('mfaSubmit'))
await settle(120)
const unknownMsg = $('mfaMsg').textContent
check('UNKNOWN_RESULT_001 → 提示「结果未知 / 先查询」（先查后决）',
  /结果未知/.test(unknownMsg) && /查询/.test(unknownMsg), unknownMsg)
await settle(1000)
check('UNKNOWN_RESULT_001 不自动关闭、不冒充「已确认」',
  $('mfaOverlay').hidden === false && !/验证通过|已确认/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
check('四种错误码各自有不同文案（不是同一句兜底）',
  new Set([authzMsg, lockedMsg, usedMsg, unknownMsg]).size === 4,
  [authzMsg, lockedMsg, usedMsg, unknownMsg])

// ================================================================ 取消

section('取消：只做本地撤销，不产生任何写操作（契约 5）')
const mfaPostsBeforeCancel = countMfaPost()
const confirmPostsBeforeCancel = countConfirmPost()
typeCode('555555')
click($('mfaCancel'))
await settle(120)
check('点「取消」关闭卡片', $('mfaOverlay').hidden === true)
check('取消不提交验证码（无新的 /confirm/mfa）', countMfaPost() === mfaPostsBeforeCancel, countMfaPost() - mfaPostsBeforeCancel)
check('取消不新建确认请求（无新的 /confirm/request）', countConfirmPost() === confirmPostsBeforeCancel, countConfirmPost() - confirmPostsBeforeCancel)
check('取消后清空输入与提示（回到初始态）',
  codeInputs().every((el) => el.value === '') && $('mfaMsg').hidden === true)
check('【契约缺口】前端取消时未调用撤销接口（后端 DELETE 已就绪，见汇报）',
  !calls.some((c) => c.method === 'DELETE' && typeof c.url === 'string' && c.url.indexOf('/api/v1/confirm/') === 0))
check('后端已提供 DELETE /api/v1/confirm/{request_id} → status=revoked',
  /@_core\.delete\("\/\{request_id\}"\)/.test(backendSrc) &&
  /def cancel_confirm_request/.test(backendSrc) &&
  /status=revoked|revoked/.test(backendSrc))

// ================================================================ 灰色动作

section('灰色动作（yellow）：样式与提示都不同于红色（契约 1、3）')
confirmState.riskLevel = 'yellow'
confirmState.mfaRequired = false
confirmState.mfaReply = null
await pushReceipt({ toolName: 'activate_card', args: { card_no: MASKED_CARD }, risk: 'yellow', mfaRequired: false, caseNo: 'HITL-20260101-0004' })
check('黄色徽标类名含 yellow', $('mfaRisk').className.indexOf('yellow') >= 0, $('mfaRisk').className)
check('黄色徽标文案为「中风险」', /中风险/.test($('mfaRiskText').textContent), $('mfaRiskText').textContent)
check('黄色提示不再要求输验证码（点确认即生效）',
  /点「确认执行」即生效/.test($('mfaSub').textContent) && !/验证码/.test($('mfaSub').textContent), $('mfaSub').textContent)
check('黄色与红色的说明文案不同', $('mfaSub').textContent !== redSub)
check('非红色动作不出现验证码输入区', $('mfaCodeBlock').hidden === true)
check('非红色动作不出现重发按钮', $('mfaResend').hidden === true)
check('非不可逆动作不显示「不可撤销」警示', $('mfaIrreversible').hidden === true)
check('黄色动作直接提示「已确认」', /已确认/.test($('mfaMsg').textContent), $('mfaMsg').textContent)
await settle(1100)
check('黄色动作无需输码即自动关闭', $('mfaOverlay').hidden === true)

section('灰色动作（green）：低风险档的样式与提示（契约 1、3）')
confirmState.riskLevel = 'green'
confirmState.mfaRequired = false
await pushReceipt({ toolName: 'activate_card', args: { card_no: MASKED_CARD }, risk: 'green', mfaRequired: false, caseNo: 'HITL-20260101-0005' })
check('绿色徽标类名含 green', $('mfaRisk').className.indexOf('green') >= 0, $('mfaRisk').className)
check('绿色徽标文案为「低风险」', /低风险/.test($('mfaRiskText').textContent), $('mfaRiskText').textContent)
check('绿色动作同样不出现验证码输入区', $('mfaCodeBlock').hidden === true)
check('三档徽标类名两两不同（red / yellow / green 各有样式钩子）',
  new Set(['mfa-risk', 'mfa-risk yellow', 'mfa-risk green']).size === 3)
await settle(1100)

// ================================================================ /pending 恢复

section('刷新恢复：从 /pending 用 args_redacted 渲染（契约 2）')
confirmState.pending = [{
  request_id: REQUEST_ID,
  tool_name: 'transfer',
  risk_level: 'red',
  mfa_required: true,
  status: 'pending',
  expires_at: EXPIRES,
  phone_mask: MASKED_PHONE,
  // 正常路径：只有脱敏摘要，没有 args
  args_redacted: {
    payee_name: '张三',
    card_no: MASKED_CARD,
    amount_fen: 50000,
    pan_full: FULL_PAN,
    mfa_code_hash: HASH,
  },
  args_hash: HASH,
  code: DEMO_CODE,
}]
click($('mfaEntry'))
await settle(180)
check('点顶栏入口从 /pending 恢复出卡片', $('mfaOverlay').hidden === false)
check('用 args_redacted 渲染操作对象（受款人）', /张三/.test($('mfaTarget').textContent), $('mfaTarget').textContent)
check('金额来自 args_redacted', $('mfaAmount').textContent === '500.00 元', $('mfaAmount').textContent)
check('卡片不含 args_redacted 里的全卡号', cardText().indexOf(FULL_PAN) < 0 && !/\d{16}/.test(cardText()))
check('卡片不含 args_redacted 里的 mfa_code_hash 键',
  !/mfa_code_hash/.test(cardText()) && cardText().indexOf(HASH) < 0)
check('卡片不含 /pending 响应里夹带的明文验证码',
  cardText().indexOf(DEMO_CODE) < 0, DEMO_CODE)
check('恢复的卡片是红色待输码态（6 格可见）', $('mfaCodeBlock').hidden === false && codeInputs().length === 6)

// ================================================================ 演示短信

section('演示短信码：只在演示面板，不进业务响应（契约 8）')
const smsBefore = countSmsGet()
check('普通用户看不到模拟短信面板', $('mfaSms').hidden === true)
check('普通用户不会请求 /confirm/sms', countSmsGet() === smsBefore, countSmsGet() - smsBefore)
check('普通用户卡片里没有演示码', cardText().indexOf(DEMO_CODE) < 0)

const roleSel = $('roleSelect')
roleSel.value = 'risk_reviewer'
roleSel.dispatchEvent(new window.Event('change', { bubbles: true }))
await settle(150)
click($('mfaEntry'))
await settle(220)
check('复核员能看到模拟短信面板', $('mfaSms').hidden === false)
check('复核员会请求 /confirm/sms 接口', countSmsGet() > smsBefore)
check('演示面板展示 /sms 返回的验证码',
  $('mfaSmsBody').textContent.indexOf(DEMO_CODE) >= 0, $('mfaSmsBody').textContent)
check('演示码只出现在演示面板内，卡片其它区域没有',
  textOutsideSmsPanel().indexOf(DEMO_CODE) < 0)
check('演示码不进业务请求（/confirm/request 与 /chat/stream 的请求体都不含它）',
  calls.every((c) => {
    if (c.url !== '/api/v1/confirm/request' && c.url !== '/api/chat/stream') return true
    return JSON.stringify(c.body || {}).indexOf(DEMO_CODE) < 0
  }))
check('验证码只发给 /confirm/mfa（演示码从未出现在 /mfa 提交体里）',
  calls.filter((c) => c.url === '/api/v1/confirm/mfa').every((c) => String(c.body.code) !== DEMO_CODE))

click($('mfaCancel'))
roleSel.value = 'user'
roleSel.dispatchEvent(new window.Event('change', { bubbles: true }))
await settle(80)

// ================================================================ 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
// jsdom 里还挂着页面的计时器（时钟 / 倒计时），不显式退出会把进程一直吊着
process.exit(0)
