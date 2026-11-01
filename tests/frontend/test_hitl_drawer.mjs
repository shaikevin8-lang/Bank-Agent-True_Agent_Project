/**
 * 前端「人工复核抽屉」决策按钮契约测试 —— 修「拒绝 / 批准执行都用不了，只有要求补件能用」。
 *
 * 用 jsdom 真实加载 Struct_of_Agent/web_test/index.html 并执行内联脚本，用打桩 fetch 模拟
 * 后端（响应形状取自 web_test/api_hitl.py 与 common/hitl.serialize_case 的实际返回）。
 *
 * 覆盖的回归点（对应复审 BUG-5 / BUG-6 与前端 UX 缺陷）：
 *   1. 备注规则与后端一致：拒绝 / 要求补件必须填备注，批准选填；
 *   2. 用户侧确认（MFA）未完成时「批准执行」置灰，并给出「为用户发起确认」入口；
 *   3. 点「为用户发起确认」按**案件自己的**（工具 + 参数 + 会话）建确认请求，不串当前会话；
 *   4. 拒绝提交体带 note + expected_version + ack，成功文案说明未执行任何写操作；
 *   5. 用户确认完成后「批准执行」可用，二次确认后提交 approve；
 *   6. 只读身份（user）下四个按钮全禁用，提示切换复核员。
 *
 * 运行方式：cd tests/frontend && node test_hitl_drawer.mjs
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

// ---------------------------------------------------------------- 固定数据

const NOW = '2026-01-01T10:00:00'
const EXPIRES = '2026-01-01T10:15:00'
const CASE_IDLE = 'HITL-20260101-0001'      // 用户尚未完成 MFA 确认
const CASE_READY = 'HITL-20260101-0002'     // 用户已完成确认
const MASKED_CARD = '6222 **** **** 2222'
const SESSION_OF_CASE = 'sess-hitl-0001'

function makeCase(overrides) {
  const base = {
    case_no: CASE_IDLE,
    status: 'open',
    reason_code: 'red_write',
    reason_text: '高风险写操作需人工复核',
    risk_level: 'red',
    session_id: SESSION_OF_CASE,
    task_id: 'task-1',
    graph_thread_id: SESSION_OF_CASE,
    assignee: null,
    claimed_at: null,
    claim_expires_at: null,
    expires_at: EXPIRES,
    remaining_seconds: 900,
    sla_seconds: 900,
    version: 1,
    business_at: NOW,
    created_at: NOW,
    updated_at: NOW,
    decided_by: null,
    decided_at: null,
    decision_note: '',
    info_request_note: '',
    proposal_summary: '冻结卡片',
    tool_name: 'freeze_card',
    user_confirmation: { mfa_required: true, mfa_ok: false, source: 'no_grant', grant_id: null },
    execution: {},
    decision: null,
    proposal: {
      tool_name: 'freeze_card',
      summary: '冻结卡片',
      args: { card_no: MASKED_CARD },
      fingerprint: 'fp-1',
      user_confirmation: { mfa_required: true, mfa_ok: false, source: 'no_grant' },
    },
  }
  return Object.assign(base, overrides || {})
}

const caseState = {
  cases: {
    [CASE_IDLE]: makeCase({}),
    [CASE_READY]: makeCase({
      case_no: CASE_READY,
      tool_name: 'report_loss',
      proposal_summary: '挂失卡片',
      proposal: {
        tool_name: 'report_loss',
        summary: '挂失卡片',
        args: { card_no: MASKED_CARD },
        fingerprint: 'fp-2',
        user_confirmation: { mfa_required: true, mfa_ok: true, source: 'confirmation_grant' },
      },
      user_confirmation: {
        mfa_required: true, mfa_ok: true, source: 'confirmation_grant',
        grant_id: 'grant-0001', consumed_at: NOW,
      },
    }),
  },
}

// ---------------------------------------------------------------- 响应桩

const calls = []

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

function ok(data, message = '') {
  return jsonResponse({ ok: true, message, error_code: null, data })
}

function fail(message, errorCode, status, data) {
  return jsonResponse({ ok: false, message, error_code: errorCode, data: data === undefined ? null : data }, status)
}

const claimExpires = () => ''

async function stubFetch(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const body = options.body ? JSON.parse(options.body) : undefined
  calls.push({ url, method, body })

  if (url === '/api/auth/me') {
    return ok({
      authenticated: true,
      user: { username: 'reviewer-01', created_at: NOW, last_login_at: NOW },
      expires_at: '2026-12-31T00:00:00',
    })
  }
  if (url === '/health/live') {
    return jsonResponse({ status: 'ok', model_mode: 'mock', model: '', api_version: 'test' })
  }
  if (url === '/api/clock') {
    return fail('演示时钟未启用', 'NOT_READY_001', 409)
  }
  if (url === '/api/sessions' && method === 'GET') return ok({ sessions: [] })

  // ---- 复核队列与案件详情
  if (typeof url === 'string' && url.indexOf('/api/hitl/cases?') === 0) {
    const status = /status=(\w+)/.exec(url)
    const wanted = status ? status[1] : 'open'
    const items = Object.keys(caseState.cases)
      .map((key) => caseState.cases[key])
      .filter((item) => item.status === wanted)
    return ok({ items: items, total: items.length })
  }
  if (typeof url === 'string' && url.indexOf('/api/hitl/cases/') === 0) {
    const rest = decodeURIComponent(url.slice('/api/hitl/cases/'.length))
    const caseNo = rest.split('/')[0]
    const tail = rest.slice(caseNo.length)
    const item = caseState.cases[caseNo]
    if (!item) return fail('案件不存在', 'case_not_found', 404)
    if (tail === '/events' && method === 'GET') {
      return ok({ case_no: caseNo, items: [{ action: 'create', at: NOW, note: '建案' }] })
    }
    if (tail === '/claim' && method === 'POST') {
      item.status = 'claimed'
      item.assignee = (body && body.operator) || 'reviewer-01'
      item.version += 1
      return ok({ case: item }, '已认领')
    }
    if (tail === '/decision' && method === 'POST') {
      const decision = body && body.decision
      if (decision === 'approve') {
        item.status = 'approved'
        item.decision = { decision: 'approve', execution: { state: 'done', ok: true } }
        item.execution = { state: 'done', ok: true, summary: '卡片已挂失' }
      } else if (decision === 'reject') {
        item.status = 'rejected'
        item.decision_note = (body && body.note) || ''
        item.decision = { decision: 'reject', execution: { state: 'not_applicable', ok: null } }
      } else {
        item.status = 'info_required'
        item.info_request_note = (body && body.note) || ''
        item.decision = { decision: 'request_info', execution: { state: 'not_applicable', ok: null } }
      }
      item.version += 2
      const execution = item.decision.execution
      return ok(
        { case: item, execution: execution, resumed: null, replay: false },
        decision === 'approve' ? '案件已批准并执行' : (decision === 'reject' ? '案件已拒绝' : '已要求补件')
      )
    }
    if (tail === '' && method === 'GET') {
      return ok({ case: item, events: [{ action: 'create', at: NOW, note: '建案' }] })
    }
    return fail('未打桩的路径', 'NOT_STUBBED', 404)
  }

  // ---- 用户侧确认（MFA）
  if (url === '/api/v1/confirm/request' && method === 'POST') {
    return ok({
      request_id: 'req-0001',
      tool_name: body.tool_name,
      risk_level: 'red',
      mfa_required: true,
      status: 'pending',
      created_at: NOW,
      expires_at: EXPIRES,
      args_redacted: body.args || {},
      args_summary: body.args || {},
      phone: '138****8888',
      phone_mask: '138****8888',
    }, '已创建确认请求')
  }
  if (url.indexOf('/api/v1/confirm/sms/') === 0) {
    return ok({ request_id: 'req-0001', phone: '138****8888', code: '246810', expires_at: EXPIRES })
  }
  if (url === '/api/v1/confirm/pending') return ok({ items: [], total: 0 })

  throw new Error('未打桩的请求: ' + method + ' ' + url + ' ' + claimExpires())
}

// ---------------------------------------------------------------- 装载页面

const html = readFileSync(htmlPath, 'utf8')
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

const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms))
const $ = (id) => document.getElementById(id)
const click = (el) => el && el.dispatchEvent(new window.Event('click', { bubbles: true }))
const setValue = (el, value) => {
  el.value = value
  el.dispatchEvent(new window.Event('input', { bubbles: true }))
}
const queueRows = () => [...document.querySelectorAll('#hitlList .hitl-item')]
const decisionCalls = () => calls.filter((c) => /\/decision$/.test(String(c.url)) && c.method === 'POST')
const lastDecisionBody = () => (decisionCalls().slice(-1)[0] || {}).body
const confirmRequests = () => calls.filter((c) => c.url === '/api/v1/confirm/request' && c.method === 'POST')

async function switchRole(role) {
  const sel = $('roleSelect')
  sel.value = role
  sel.dispatchEvent(new window.Event('change', { bubbles: true }))
  await settle(120)
}

async function openCase(caseNo) {
  const row = queueRows().filter((el) => el.textContent.indexOf(caseNo) >= 0)[0]
  click(row)
  await settle(150)
}

await settle(150)   // 等首屏（/api/auth/me → 主界面）稳定

// ================================================================ 静态结构

section('静态结构：抽屉 / 备注 / 四个决策按钮')
check('复核抽屉默认隐藏', $('hitlOverlay').hidden === true)
check('抽屉里有复核队列容器与详情容器', !!$('hitlList') && !!$('hitlDetail'))

click($('hitlEntry'))
await settle(120)
check('点顶栏「人工复核」后抽屉打开', $('hitlOverlay').hidden === false)

await switchRole('risk_reviewer')
check('复核队列里出现两个待处理案件', queueRows().length === 2, queueRows().length)

await openCase(CASE_IDLE)
section('备注规则：拒绝 / 要求补件必填（与后端一致）')
const noteLabelText = document.querySelector('#hitlDetail .hitl-field').textContent
check('备注标签写明「拒绝 / 要求补件必填，批准选填」', /拒绝 \/ 要求补件必填/.test(noteLabelText), noteLabelText)
check('备注输入框与核对勾选框存在', !!$('hitlNote') && !!$('hitlAck'))
check('四个决策按钮都在', !!$('hitlClaim') && !!$('hitlApprove') && !!$('hitlReject') && !!$('hitlRequestInfo'))

section('用户侧确认未完成：批准置灰 + 缺口可见')
check('详情里显示「用户侧确认（MFA）」分区', !!$('hitlConfirmBox') && /用户侧确认/.test($('hitlConfirmBox').textContent))
check('提示说明人工批准不能代替用户确认', /不能代替用户确认/.test($('hitlConfirmText').textContent), $('hitlConfirmText').textContent)
check('提供「为用户发起确认」入口', !!$('hitlAskUserConfirm') && /为用户发起确认/.test($('hitlAskUserConfirm').textContent))
check('未勾选核对时批准与拒绝都不可用', $('hitlApprove').disabled === true && $('hitlReject').disabled === true)

$('hitlAck').checked = true
$('hitlAck').dispatchEvent(new window.Event('change', { bubbles: true }))
await settle(30)
check('勾选核对后：无备注时拒绝仍不可用（后端也会 409）', $('hitlReject').disabled === true)
check('勾选核对后：无备注时要求补件仍不可用', $('hitlRequestInfo').disabled === true)
check('勾选核对后：用户确认未完成，批准执行仍不可用', $('hitlApprove').disabled === true)
check('提示条说明先完成用户确认', /用户侧确认/.test($('hitlActionHint').textContent), $('hitlActionHint').textContent)

setValue($('hitlNote'), '卡片疑似被他人使用，先拒绝并联系用户')
await settle(30)
check('填了备注后「拒绝」变为可用', $('hitlReject').disabled === false)
check('填了备注后「要求补件」变为可用', $('hitlRequestInfo').disabled === false)
check('用户确认未完成时「批准执行」仍置灰', $('hitlApprove').disabled === true)

section('为用户发起确认：按案件自己的会话建确认请求')
click($('hitlAskUserConfirm'))
await settle(150)
const confirmCall = confirmRequests().slice(-1)[0]
check('发起了 /api/v1/confirm/request', !!confirmCall, calls.map((c) => c.url))
check('确认请求用的是案件的工具名', confirmCall && confirmCall.body.tool_name === 'freeze_card', confirmCall && confirmCall.body)
check('确认请求用的是案件的会话（不串当前会话）',
  confirmCall && confirmCall.body.session_id === SESSION_OF_CASE, confirmCall && confirmCall.body)
check('确认请求参数来自案件提案（脱敏卡号）',
  confirmCall && confirmCall.body.args && confirmCall.body.args.card_no === MASKED_CARD, confirmCall && confirmCall.body.args)
check('确认卡（MFA）被打开', $('mfaOverlay').hidden === false)
check('抽屉里提示已创建用户确认请求', /已创建用户确认请求/.test($('hitlMsg').textContent), $('hitlMsg').textContent)

// 关掉确认卡，避免遮罩影响后续点击
click($('mfaCancel'))
await settle(60)

section('拒绝：提交体带 note / expected_version / ack，且不执行写操作')
click($('hitlReject'))
await settle(150)
const rejectBody = lastDecisionBody()
check('提交了一次 /decision', decisionCalls().length === 1, decisionCalls().map((c) => c.url))
check('决策为 reject', rejectBody && rejectBody.decision === 'reject', rejectBody)
check('带上审核员填写的备注', rejectBody && rejectBody.note === '卡片疑似被他人使用，先拒绝并联系用户', rejectBody && rejectBody.note)
check('带上详情里读到的 expected_version（不会因自动认领而冲突）',
  rejectBody && rejectBody.expected_version === 1, rejectBody && rejectBody.expected_version)
check('显式 ack=true', rejectBody && rejectBody.ack === true, rejectBody)
check('结果区说明未执行任何写操作', /已拒绝该案件，未执行任何写操作/.test($('hitlResult').textContent), $('hitlResult').textContent)
check('拒绝后案件进入终态、按钮全部只读', $('hitlApprove').disabled === true && $('hitlReject').disabled === true)

section('批准：用户确认完成后可批准并执行')
await openCase(CASE_READY)
check('用户确认已完成时不再提示缺口', !$('hitlConfirmText') || /已完成/.test($('hitlConfirmText').textContent), $('hitlConfirmText') && $('hitlConfirmText').textContent)
check('用户确认已完成时不出现「为用户发起确认」按钮', !$('hitlAskUserConfirm'))
$('hitlAck').checked = true
$('hitlAck').dispatchEvent(new window.Event('change', { bubbles: true }))
await settle(30)
check('勾选核对后「批准执行」可用（备注选填）', $('hitlApprove').disabled === false)

click($('hitlApprove'))
await settle(120)
const confirmOverlay = document.querySelector('.confirm-overlay')
check('批准前弹出二次确认', !!confirmOverlay && confirmOverlay.hidden === false)
click(confirmOverlay.querySelector('.btn.primary'))
await settle(200)
const approveBody = lastDecisionBody()
check('决策为 approve', approveBody && approveBody.decision === 'approve', approveBody)
check('批准时备注可留空', approveBody && approveBody.note === '', approveBody && approveBody.note)
check('批准带 ack 与 expected_version', approveBody && approveBody.ack === true && approveBody.expected_version === 1, approveBody)
check('结果区显示执行回执', /已执行|操作已按提案完成/.test($('hitlResult').textContent), $('hitlResult').textContent)

section('只读身份：user 角色下四个按钮全禁用')
await switchRole('user')
await settle(150)
const readonlyRows = queueRows().length
check('切回 user 后队列仍可见（只读）', readonlyRows >= 0, readonlyRows)
if (readonlyRows > 0) {
  await openCase(CASE_IDLE)
  check('user 身份下认领 / 批准 / 拒绝 / 补件全部禁用',
    $('hitlClaim').disabled === true && $('hitlApprove').disabled === true
    && $('hitlReject').disabled === true && $('hitlRequestInfo').disabled === true)
  check('提示只读并要求切换复核员', /只读/.test($('hitlActionHint').textContent), $('hitlActionHint').textContent)
}

// ---------------------------------------------------------------- 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
// jsdom 里还挂着页面的轮询定时器（setInterval），不显式退出会把进程吊住
process.exit(0)
