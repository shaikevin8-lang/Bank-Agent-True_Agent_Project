/**
 * 前端「登录 / 注册门禁」的 DOM 集成测试。
 *
 * 用 jsdom 真实加载 web_test/index.html 并执行其中的内联脚本，再用打桩的 fetch
 * 模拟后端（按 docs/_work/登录注册-前后端契约.md 的响应格式），验证：
 * 门禁的 DOM 结构、未登录时的启动流程（401 不白屏、不拉业务数据、门禁关不掉）、
 * 登录成功 / 失败、注册的本地校验与成功后的页签回切、提交中的按钮状态、
 * 以及退出登录后清空站点状态并回到门禁页。
 *
 * 运行方式（Node 18+）：
 *     cd tests/frontend && npm install jsdom && node test_auth_gate.mjs
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

const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------- 打桩后端
//
// 只桩住门禁自己会用到的接口 + 登录成功后主界面必然要拉的骨架数据；
// 其余（演示时钟 / 人工复核队列）故意让它失败 —— 页面按契约必须静默降级。

const NOW = '2026-01-01T10:00:00'
const EXPIRES = '2026-12-31T23:59:59'

const HEALTH = {
  status: 'ok', model_mode: 'mock', model: '', base_url: '', search_configured: false,
  api_version: 'test',
}

const LLM_SNAPSHOT = {
  configured: false, mode: 'mock', source: 'none', base_url: '', model: '',
  response_format: 'text', temperature: 0.0, max_tokens: 1024, timeout: 0.0,
  api_key_configured: false, api_key_masked: '', env_api_key_present: false,
  updated_at: null, active_model_class: 'MockChat',
}

const SEARCH_SNAPSHOT = {
  configured: false, mode: 'unconfigured', source: 'none', base_url: '',
  max_results: 5, timeout: 10.0, api_key_configured: false, api_key_masked: '',
  env_api_key_present: false, updated_at: null,
}

const calls = []
const scenario = {
  meUser: null,          // null = 未登录（/api/auth/me 返回 401）
  loginFails: false,
  loginDelayMs: 0,       // 用来观察「提交中」的按钮状态
  registered: [],        // 已注册账号（小写）
}
let sessionRows = []

function resetScenario() {
  scenario.meUser = null
  scenario.loginFails = false
  scenario.loginDelayMs = 0
  scenario.registered = []
  sessionRows = [{
    id: 'web-a1', title: '会话甲', created_at: NOW, updated_at: NOW,
    message_count: 1, preview: '甲', title_set_by_user: false,
  }]
  calls.length = 0
}

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

const ok = (message, data) => jsonResponse({ ok: true, message: message || '', error_code: null, data })
const fail = (message, errorCode, status) =>
  jsonResponse({ ok: false, message, error_code: errorCode, data: null }, status)

function userEnvelope(username) {
  return {
    authenticated: true,
    user: { username, created_at: NOW, last_login_at: NOW },
    expires_at: EXPIRES,
  }
}

async function stubFetch(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const body = options.body ? JSON.parse(options.body) : undefined
  calls.push({ url, method, body, credentials: options.credentials })

  // ---- 认证四条 ----
  if (url === '/api/auth/me') {
    if (!scenario.meUser) return fail('登录状态已失效，请重新登录', 'AUTH_UNAUTHORIZED', 401)
    return ok('', userEnvelope(scenario.meUser))
  }
  if (url === '/api/auth/login' && method === 'POST') {
    if (scenario.loginDelayMs) await new Promise((r) => setTimeout(r, scenario.loginDelayMs))
    if (!scenario.loginFails && body.username === 'alice' && body.password === 'secret123') {
      return ok('登录成功', { user: { username: body.username, created_at: NOW, last_login_at: NOW }, expires_at: EXPIRES })
    }
    return fail('账号名或密码不正确', 'AUTH_BAD_CREDENTIALS', 401)
  }
  if (url === '/api/auth/register' && method === 'POST') {
    const name = String((body && body.username) || '').trim().toLowerCase()
    if (scenario.registered.indexOf(name) >= 0) {
      return fail('该账号名已被注册，请换一个', 'AUTH_USERNAME_TAKEN', 409)
    }
    scenario.registered.push(name)
    return jsonResponse({
      ok: true, message: '注册成功，请使用该账号登录', error_code: null,
      data: { user: { username: body.username, created_at: NOW } },
    }, 201)
  }
  if (url === '/api/auth/logout' && method === 'POST') {
    scenario.meUser = null
    return ok('已退出登录', { logged_out: true })
  }

  // ---- 登录后主界面要用的骨架数据 ----
  if (url === '/api/sessions' && method === 'GET') {
    return ok('', { sessions: sessionRows.map((s) => ({ ...s })) })
  }
  if (url.indexOf('/api/sessions/') === 0 && method === 'GET') {
    const id = decodeURIComponent(url.slice('/api/sessions/'.length))
    const row = sessionRows.filter((s) => s.id === id)[0]
    if (!row) return fail('会话不存在或已被删除', 'SESSION_NOT_FOUND', 404)
    return ok('', {
      session: { ...row },
      messages: [{ role: 'user', content: '甲', created_at: NOW, agent: null, model_mode: null, model_name: null, tools: [] }],
    })
  }
  if (url === '/health/live') return jsonResponse({ ...HEALTH })
  if (url === '/api/settings/llm') return ok('', LLM_SNAPSHOT)
  if (url === '/api/settings/search') return ok('', SEARCH_SNAPSHOT)
  throw new Error('未打桩的请求: ' + method + ' ' + url)
}

// ---------------------------------------------------------------- 页面装载

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
  // 启动时要先等 /api/auth/me 回来，再决定进主界面还是停在门禁页
  await settle(80)
  return dom
}

/** 装一份「未登录」的新页面（每次 reset 后端状态）。 */
async function loadGatePage() {
  resetScenario()
  return loadPage()
}

/** 装一份「已登录」的新页面：/api/auth/me 直接返回登录信封。 */
async function loadSignedInPage(username = 'alice') {
  resetScenario()
  scenario.meUser = username
  return loadPage()
}

const $ = (w, id) => w.document.getElementById(id)
const click = (w, el) => el.dispatchEvent(new w.Event('click', { bubbles: true }))
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', { key: k, bubbles: true }))
const submitForm = (w) => $(w, 'authForm').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }))
const fill = (w, values) => {
  if (values.username !== undefined) $(w, 'authUsername').value = values.username
  if (values.password !== undefined) $(w, 'authPassword').value = values.password
  if (values.confirm !== undefined) $(w, 'authConfirmPassword').value = values.confirm
}
const gateVisible = (w) => $(w, 'authGate').hidden === false
const appVisible = (w) => $(w, 'app').hidden === false
const errText = (w) => { const el = $(w, 'authError'); return el.hidden ? '' : el.textContent }
const hintText = (w) => { const el = $(w, 'authHint'); return el.hidden ? '' : el.textContent }
const posted = (url) => calls.some((c) => c.url === url && c.method === 'POST')

// ---------------------------------------------------------------- 1. 结构

section('门禁结构：契约要求的固定 id 与语义')
{
  const dom = await loadGatePage()
  const w = dom.window
  const d = w.document

  ;['authGate', 'authForm', 'authTabLogin', 'authTabRegister', 'authUsername', 'authPassword',
    'authConfirmField', 'authConfirmPassword', 'authError', 'authSubmit', 'authHint',
    'authUser', 'logoutBtn'].forEach((id) => check('存在 #' + id, !!$(w, id)))

  check('页签容器用 role=tablist', !!d.querySelector('#authGate [role="tablist"]'))
  check('两个页签都是 role=tab',
    $(w, 'authTabLogin').getAttribute('role') === 'tab'
    && $(w, 'authTabRegister').getAttribute('role') === 'tab')
  check('#authError 用 role=alert', $(w, 'authError').getAttribute('role') === 'alert')
  check('账号名 / 密码用 label 包裹（点击文字可聚焦输入框）',
    !!d.querySelector('label[for="authUsername"]') && !!d.querySelector('label[for="authPassword"]'))
  check('两个密码框都是 password 类型',
    $(w, 'authPassword').type === 'password' && $(w, 'authConfirmPassword').type === 'password')
  check('登录页签默认选中',
    $(w, 'authTabLogin').getAttribute('aria-selected') === 'true'
    && $(w, 'authTabLogin').classList.contains('active'))
  check('登录模式下不显示确认密码', $(w, 'authConfirmField').hidden === true)
  check('提交按钮文案为「登录」', $(w, 'authSubmit').textContent.trim() === '登录')
  check('门禁里没有任何关闭入口',
    !d.querySelector('#authGate [data-act="close"], #authGate .auth-close')
    && !/关闭/.test($(w, 'authGate').textContent))

  const css = d.querySelector('style').textContent.replace(/\/\*[\s\S]*?\*\//g, '')
  check('样式表补了 .app[hidden]{display:none}（否则 .app{display:flex} 会盖掉 [hidden]）',
    /\.app\[hidden\][^{]*\{[^}]*display:\s*none/.test(css))
  check('门禁是 fixed + inset:0 铺满全屏',
    /\.auth-gate\s*\{[^}]*position:\s*fixed/.test(css) && /\.auth-gate\s*\{[^}]*inset:\s*0/.test(css))
  const gateZ = /\.auth-gate\s*\{[^}]*z-index:\s*(\d+)/.exec(css)
  const confirmZ = /\.confirm-overlay\s*\{[^}]*z-index:\s*(\d+)/.exec(css)
  check('门禁层级高于自绘确认框（不会漏出下面的浮层）',
    !!gateZ && !!confirmZ && Number(gateZ[1]) > Number(confirmZ[1]),
    [gateZ && gateZ[1], confirmZ && confirmZ[1]])
  check('窄屏下卡片宽度自适应（92vw）', /@media[^{]*\{[\s\S]*auth-card\s*\{[^}]*92vw/.test(css))
}

// ---------------------------------------------------------------- 2. 未登录

section('/api/auth/me 401：停在门禁页，不拉任何业务数据')
{
  const dom = await loadGatePage()
  const w = dom.window
  const d = w.document

  check('问过 /api/auth/me', calls.some((c) => c.url === '/api/auth/me'))
  const meCall = calls.filter((c) => c.url === '/api/auth/me')[0]
  check('/api/auth/me 带 same-origin 凭据', meCall && meCall.credentials === 'same-origin',
    meCall && meCall.credentials)
  check('门禁可见', gateVisible(w))
  check('#app 被隐藏（带 hidden 属性）',
    $(w, 'app').hidden === true && $(w, 'app').hasAttribute('hidden'))
  check('未登录时不拉会话列表', !calls.some((c) => c.url === '/api/sessions'))
  check('未登录时顶栏不显示账号名 / 退出按钮',
    $(w, 'authUser').hidden === true && $(w, 'logoutBtn').hidden === true)
  check('错误位默认隐藏', $(w, 'authError').hidden === true)
  check('登录页签为默认态', $(w, 'authTabLogin').classList.contains('active'))

  // 门禁不可关闭：Esc / 点遮罩都不能放行
  d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  click(w, $(w, 'authGate'))
  await settle()
  check('Esc 与点遮罩都不能关掉门禁', gateVisible(w) && $(w, 'app').hidden === true)
}

// ---------------------------------------------------------------- 3. 登录成功

section('登录成功：门禁收起、主界面起来、顶栏显示账号名')
{
  const dom = await loadGatePage()
  const w = dom.window
  const d = w.document

  fill(w, { username: 'alice', password: 'secret123' })
  calls.length = 0
  // 回车提交（不是点按钮）：门禁只绑了一次 submit + 一次 keydown
  key(w, $(w, 'authPassword'), 'Enter')
  await settle(160)

  const loginCall = calls.filter((c) => c.url === '/api/auth/login')[0]
  check('回车提交发出 POST /api/auth/login', !!loginCall && loginCall.method === 'POST')
  check('登录请求体带账号名与密码',
    !!loginCall && loginCall.body.username === 'alice' && loginCall.body.password === 'secret123',
    loginCall && loginCall.body)
  check('登录请求带 same-origin 凭据', !!loginCall && loginCall.credentials === 'same-origin')
  check('门禁已隐藏', $(w, 'authGate').hidden === true)
  check('#app 已显示', appVisible(w))
  check('顶栏显示登录账号名',
    $(w, 'authUser').textContent === 'alice' && $(w, 'authUser').hidden === false,
    $(w, 'authUser').textContent)
  check('「退出登录」按钮出现', $(w, 'logoutBtn').hidden === false)
  check('currentOperator 同步为登录账号', w.eval('currentOperator') === 'alice', w.eval('currentOperator'))
  check('登录后拉取会话列表', calls.some((c) => c.url === '/api/sessions' && c.method === 'GET'))
  check('会话列表渲染出历史会话', d.querySelectorAll('.session-item').length === 1,
    d.querySelectorAll('.session-item').length)
  check('登录成功不留下错误提示', $(w, 'authError').hidden === true)
}

// ---------------------------------------------------------------- 4. 注册本地校验

section('注册：本地校验拦住，不发 POST /api/auth/register')
{
  const dom = await loadGatePage()
  const w = dom.window

  click(w, $(w, 'authTabRegister'))
  await settle()
  check('切到注册页签后显示确认密码', $(w, 'authConfirmField').hidden === false)
  check('切到注册页签后按钮文案变「注册」', $(w, 'authSubmit').textContent.trim() === '注册')
  check('注册页签 aria-selected 同步', $(w, 'authTabRegister').getAttribute('aria-selected') === 'true')

  // 两次密码不一致
  fill(w, { username: 'bob', password: 'secret123', confirm: 'secret124' })
  calls.length = 0
  submitForm(w)
  await settle(80)
  check('两次密码不一致时不发注册请求', !posted('/api/auth/register'))
  check('#authError 提示两次密码不一致', /不一致/.test(errText(w)), errText(w))
  check('仍停在门禁页', gateVisible(w) && $(w, 'app').hidden === true)

  // 账号名太短
  fill(w, { username: 'bo', password: 'secret123', confirm: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(80)
  check('账号名过短时不发注册请求', !posted('/api/auth/register'))
  check('提示账号名规则', /账号名/.test(errText(w)) && /3–32/.test(errText(w)), errText(w))

  // 账号名含空格
  fill(w, { username: 'a b c', password: 'secret123', confirm: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(80)
  check('账号名含空格时不发注册请求', !posted('/api/auth/register'))
  check('提示不能包含空格', /空格/.test(errText(w)), errText(w))

  // 密码太短
  fill(w, { username: 'bobby', password: '123', confirm: '123' })
  calls.length = 0
  submitForm(w)
  await settle(80)
  check('密码过短时不发注册请求', !posted('/api/auth/register'))
  check('提示密码长度规则', /密码/.test(errText(w)) && /6–64/.test(errText(w)), errText(w))
}

// ---------------------------------------------------------------- 5. 注册成功

section('注册成功：切回登录页签、预填账号名、清空密码')
{
  const dom = await loadGatePage()
  const w = dom.window

  click(w, $(w, 'authTabRegister'))
  await settle()
  fill(w, { username: 'carol', password: 'secret123', confirm: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(120)

  const regCall = calls.filter((c) => c.url === '/api/auth/register')[0]
  check('发出 POST /api/auth/register', !!regCall && regCall.method === 'POST')
  check('注册请求体带 confirm_password',
    !!regCall && regCall.body.confirm_password === 'secret123', regCall && regCall.body)
  check('成功后切回登录页签',
    $(w, 'authTabLogin').classList.contains('active')
    && $(w, 'authTabLogin').getAttribute('aria-selected') === 'true'
    && $(w, 'authTabRegister').getAttribute('aria-selected') === 'false')
  check('确认密码字段重新隐藏', $(w, 'authConfirmField').hidden === true)
  check('按钮文案恢复为「登录」', $(w, 'authSubmit').textContent.trim() === '登录')
  check('账号名已预填', $(w, 'authUsername').value === 'carol', $(w, 'authUsername').value)
  check('两个密码框都清空',
    $(w, 'authPassword').value === '' && $(w, 'authConfirmPassword').value === '')
  check('#authHint 提示注册成功，请使用该账号登录',
    $(w, 'authHint').hidden === false && /注册成功/.test(hintText(w)), hintText(w))
  check('注册不自动登录（仍在门禁页）', gateVisible(w) && $(w, 'app').hidden === true)
  check('注册成功后不残留错误提示', $(w, 'authError').hidden === true)

  // 同一个账号名再注册一次 → 展示后端 message
  click(w, $(w, 'authTabRegister'))
  await settle()
  fill(w, { username: 'carol', password: 'secret123', confirm: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(100)
  check('重名时展示后端 message', /已被注册/.test(errText(w)), errText(w))
  check('重名失败后仍停在注册页签', $(w, 'authTabRegister').classList.contains('active'))
}

// ---------------------------------------------------------------- 6. 登录失败

section('登录失败：停在门禁页并显示后端 message')
{
  const dom = await loadGatePage()
  const w = dom.window

  fill(w, { username: 'alice', password: 'wrong-password' })
  calls.length = 0
  submitForm(w)
  await settle(100)

  check('发出 POST /api/auth/login', posted('/api/auth/login'))
  check('门禁仍可见', gateVisible(w))
  check('#app 仍然隐藏', $(w, 'app').hidden === true)
  check('显示后端 message', errText(w) === '账号名或密码不正确', errText(w))
  check('错误位 role=alert 可见', $(w, 'authError').hidden === false)
  check('按钮恢复可点且文案回到「登录」',
    $(w, 'authSubmit').disabled === false && $(w, 'authSubmit').textContent.trim() === '登录')
  check('没有误拉会话列表', !calls.some((c) => c.url === '/api/sessions'))
}

// ---------------------------------------------------------------- 7. 提交中

section('提交中：禁用按钮并改文案')
{
  const dom = await loadGatePage()
  const w = dom.window

  scenario.loginDelayMs = 150
  fill(w, { username: 'alice', password: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(40)

  check('提交中按钮被禁用', $(w, 'authSubmit').disabled === true)
  check('提交中文案为「登录中…」', $(w, 'authSubmit').textContent.trim() === '登录中…',
    $(w, 'authSubmit').textContent)
  check('提交中不会重复发请求', calls.filter((c) => c.url === '/api/auth/login').length === 1)

  await settle(240)
  check('登录成功后门禁收起', $(w, 'authGate').hidden === true && appVisible(w))
  check('成功后按钮恢复可点', $(w, 'authSubmit').disabled === false)
  scenario.loginDelayMs = 0
}

// ---------------------------------------------------------------- 8. 退出登录

section('退出登录：清空站点状态并回到门禁页')
{
  const dom = await loadSignedInPage('alice')
  const w = dom.window
  const d = w.document

  check('已登录时门禁隐藏', $(w, 'authGate').hidden === true)
  check('已登录时 #app 可见', appVisible(w))
  check('顶栏账号名来自 /api/auth/me',
    $(w, 'authUser').textContent === 'alice' && $(w, 'authUser').hidden === false)
  check('已登录时未再显示登录卡片', $(w, 'authGate').hasAttribute('hidden'))

  // 制造一些「会残留」的状态：对话区内容 + localStorage 里的当前会话
  $(w, 'chat').innerHTML = '<div class="bubble">上一个账号的残留内容</div>'
  w.localStorage.setItem('bank_agent.active_session', 'web-a1')

  calls.length = 0
  click(w, $(w, 'logoutBtn'))
  await settle(140)

  check('发出 POST /api/auth/logout', posted('/api/auth/logout'))
  check('回到门禁页', gateVisible(w))
  check('#app 重新隐藏', $(w, 'app').hidden === true)
  check('聊天区 DOM 被清空', $(w, 'chat').textContent === '', $(w, 'chat').textContent)
  check('localStorage 里的当前会话被清掉',
    !w.localStorage.getItem('bank_agent.active_session'),
    w.localStorage.getItem('bank_agent.active_session'))
  check('顶栏账号名与退出按钮一起隐藏',
    $(w, 'authUser').hidden === true && $(w, 'logoutBtn').hidden === true)
  check('提示已退出登录', /已退出登录/.test(hintText(w)), hintText(w))
  check('登录页签为默认态', $(w, 'authTabLogin').classList.contains('active'))

  // 退出后能再次登录（started 标志必须复位，否则主界面起不来）
  fill(w, { username: 'alice', password: 'secret123' })
  calls.length = 0
  submitForm(w)
  await settle(160)
  check('退出后可以重新登录', $(w, 'authGate').hidden === true && appVisible(w))
  check('重新登录后再次拉取会话列表', calls.some((c) => c.url === '/api/sessions'))
  check('重新登录后账号名照常显示', $(w, 'authUser').textContent === 'alice')
}

// ---------------------------------------------------------------- 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
// jsdom 里还挂着页面的演示时钟轮询（setInterval），不显式退出会把进程一直吊着
process.exit(0)
